try:
    import indigo
except ImportError:
    pass

import json
import re
import socket
import time
import urllib.request
import urllib.error
from datetime import datetime


PLUGIN_VERSION      = "1.1.4"
TOKEN_LIFETIME_SECS = 1800   # re-authenticate after 30 minutes

# States defined in Devices.xml — always present, never stored in discoveredStates
_BASE_STATES = frozenset({'communicationStatus', 'systemStateText', 'lastUpdate'})


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        # self.debug controls whether self.logger.debug() messages appear in the
        # Indigo Event Log.  Do NOT touch indigo_log_handler levels — that breaks logging.
        self.debug = pluginPrefs.get('showDebugInfo', False)

        # Per-device token cache: {dev_id: {'token': str, 'time': float}}
        self._tokens: dict = {}

    # ------------------------------------------------------------------
    # Dynamic state management
    # ------------------------------------------------------------------

    def getDeviceStateList(self, dev):
        """
        Indigo calls this whenever stateListOrDisplayStateIdChanged() is invoked.
        Returns the base states from Devices.xml PLUS all states discovered so far
        from live API data (stored in pluginProps['discoveredStates']).
        States whose API values were null are not registered until they first appear.
        """
        state_list = indigo.PluginBase.getDeviceStateList(self, dev)

        for key, type_name in sorted(self._get_discovered_states(dev).items()):
            label    = self._camel_to_label(key)
            cp_label = label
            if type_name == 'bool':
                state_list.append(self.getDeviceStateDictForBoolTrueFalseType(key, label, cp_label))
            elif type_name == 'number':
                state_list.append(self.getDeviceStateDictForNumberType(key, label, cp_label))
            else:
                state_list.append(self.getDeviceStateDictForStringType(key, label, cp_label))

        return state_list

    @staticmethod
    def _camel_to_label(key):
        """batteryHealthIndex → Battery Health Index"""
        s = re.sub(r'([A-Z])', r' \1', key).strip()
        return s[0].upper() + s[1:] if s else key

    @staticmethod
    def _infer_type(value):
        """Infer state type from the Python value being stored."""
        if isinstance(value, bool):
            return 'bool'
        if isinstance(value, (int, float)):
            return 'number'
        return 'string'

    def _get_discovered_states(self, dev):
        """Return {key: type_name} for all previously discovered dynamic states."""
        try:
            return json.loads(dev.pluginProps.get('discoveredStates', '{}'))
        except Exception:
            return {}

    def _register_new_states(self, dev, new_state_dicts):
        """
        Persist new key/type pairs to pluginProps['discoveredStates'] and call
        stateListOrDisplayStateIdChanged() so Indigo calls getDeviceStateList()
        and registers the new states before we try to write them.
        Returns the re-fetched device object (now with updated states).
        """
        discovered = self._get_discovered_states(dev)
        added = {
            s['key']: self._infer_type(s['value'])
            for s in new_state_dicts
            if s['key'] not in _BASE_STATES and s['key'] not in discovered
        }

        if not added:
            return dev

        self.logger.debug(
            f"{dev.name}: registering {len(added)} new dynamic state(s): {sorted(added)}"
        )
        discovered.update(added)
        props = dev.pluginProps
        props['discoveredStates'] = json.dumps(discovered)
        dev.replacePluginPropsOnServer(props)
        dev.stateListOrDisplayStateIdChanged()
        return indigo.devices.get(dev.id)

    def startup(self):
        self.logger.info(f"Cyberpower UPS Panel v{PLUGIN_VERSION} starting")

    def shutdown(self):
        self.logger.info("Cyberpower UPS Panel stopping")

    def runConcurrentThread(self):
        try:
            while True:
                self._poll_all_devices()
                interval = int(self.pluginPrefs.get('updateInterval', 60))
                self.sleep(interval)
        except self.StopThread:
            pass

    # ------------------------------------------------------------------
    # Plugin preferences
    # ------------------------------------------------------------------

    def validatePrefsConfigUi(self, valuesDict):
        errorsDict = indigo.Dict()
        try:
            interval = int(valuesDict.get('updateInterval', 60))
            if interval < 10:
                errorsDict['updateInterval'] = "Minimum interval is 10 seconds"
        except ValueError:
            errorsDict['updateInterval'] = "Must be a whole number"
        if len(errorsDict) > 0:
            return (False, valuesDict, errorsDict)
        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.debug = valuesDict.get('showDebugInfo', False)
            self.logger.debug("Debug logging enabled")

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------

    def deviceStartComm(self, dev):
        super().deviceStartComm(dev)
        self.logger.info(f"UPS device starting: {dev.name}")
        dev.stateListOrDisplayStateIdChanged()
        self._update_device(dev)

    def deviceStopComm(self, dev):
        self.logger.info(f"UPS device stopping: {dev.name}")
        self._tokens.pop(dev.id, None)

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errorsDict = indigo.Dict()
        if not valuesDict.get('host', '').strip():
            errorsDict['host'] = "Host address is required"
        if not valuesDict.get('port', '').strip():
            errorsDict['port'] = "Port is required"
        if not valuesDict.get('username', '').strip():
            errorsDict['username'] = "Username is required"
        if not valuesDict.get('password', '').strip():
            errorsDict['password'] = "Password is required"
        if len(errorsDict) > 0:
            return (False, valuesDict, errorsDict)
        return (True, valuesDict)

    def deviceUpdated(self, origDev, newDev):
        # Drop cached token if credentials changed so next poll re-authenticates
        if (origDev.pluginProps.get('host')     != newDev.pluginProps.get('host') or
                origDev.pluginProps.get('username') != newDev.pluginProps.get('username') or
                origDev.pluginProps.get('password') != newDev.pluginProps.get('password')):
            self.logger.debug(f"{newDev.name}: credentials changed — dropping cached token")
            self._tokens.pop(newDev.id, None)
        super().deviceUpdated(origDev, newDev)

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def forceUpdate(self):
        self.logger.info("Forcing UPS status update on all devices")
        self._tokens.clear()
        self._poll_all_devices()

    def toggleDebugEnabled(self):
        self.debug = not self.debug
        self.pluginPrefs['showDebugInfo'] = self.debug
        state = "enabled" if self.debug else "disabled"
        self.logger.info(f"Debug logging {state}")

    # ------------------------------------------------------------------
    # API: authentication (per device)
    # ------------------------------------------------------------------

    def _authenticate(self, dev):
        props    = dev.pluginProps
        host     = props.get('host', '').strip()
        port     = props.get('port', '3052').strip()
        username = props.get('username', '').strip()
        password = props.get('password', '').strip()

        if not host or not username or not password:
            self.logger.error(f"{dev.name}: host/username/password missing — check device settings")
            return False

        url     = f"http://{host}:{port}/local/rest/v1/login/verify"
        payload = json.dumps({"userName": username, "password": password}).encode('utf-8')

        self.logger.debug(f"{dev.name}: authenticating at {url} as '{username}'")

        req = urllib.request.Request(
            url,
            data    = payload,
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json'},
            method  = 'POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('utf-8').strip()
                self.logger.debug(f"{dev.name}: login raw response: {raw!r}")

                # Response body is a JSON string: "Bearer eyJhbG..."
                # json.loads strips the outer quotes → "Bearer eyJhbG..."
                # which is the complete Authorization header value (no extra prefix needed).
                try:
                    token = json.loads(raw)
                except Exception:
                    token = raw.strip('"')

                if token:
                    self._tokens[dev.id] = {'token': token, 'time': time.time()}
                    self.logger.info(f"{dev.name}: authenticated successfully")
                    return True

                self.logger.error(f"{dev.name}: login returned empty token — response was: {raw!r}")
                return False

        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8')
            except Exception:
                pass
            self.logger.error(f"{dev.name}: login HTTP {e.code} {e.reason} — {body}")

        except urllib.error.URLError as e:
            self.logger.error(f"{dev.name}: cannot reach {host}:{port} — {e.reason}")

        except (TimeoutError, socket.timeout):
            self.logger.error(f"{dev.name}: login timed out after 10s — host unreachable or restarting")

        except Exception:
            self.logger.exception(f"{dev.name}: unexpected error during authentication")

        self._tokens.pop(dev.id, None)
        return False

    def _ensure_token(self, dev):
        entry = self._tokens.get(dev.id)
        if entry is None or (time.time() - entry['time']) > TOKEN_LIFETIME_SECS:
            self.logger.debug(f"{dev.name}: token missing or expired — re-authenticating")
            return self._authenticate(dev)
        return True

    # ------------------------------------------------------------------
    # API: generic POST (per device)
    # ------------------------------------------------------------------

    def _api_post(self, dev, path, payload, _retrying=False):
        """POST JSON payload to the given path, handling auth the same way as status fetch."""
        if not self._ensure_token(dev):
            return None

        props = dev.pluginProps
        host  = props.get('host', '').strip()
        port  = props.get('port', '3052').strip()
        url   = f"http://{host}:{port}{path}"
        token = self._tokens[dev.id]['token']

        self.logger.debug(f"{dev.name}: POST {url} payload={payload!r}")

        req = urllib.request.Request(
            url,
            data    = json.dumps(payload).encode('utf-8'),
            headers = {
                'Authorization': token,
                'Content-Type' : 'application/json',
                'Accept'       : 'application/json',
            },
            method  = 'POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('utf-8').strip()
                self.logger.debug(f"{dev.name}: POST response: {raw!r}")
                return raw or True

        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not _retrying:
                self.logger.debug(f"{dev.name}: HTTP {e.code} on POST — re-authenticating")
                self._tokens.pop(dev.id, None)
                if self._authenticate(dev):
                    return self._api_post(dev, path, payload, _retrying=True)
            body = ''
            try:
                body = e.read().decode('utf-8')
            except Exception:
                pass
            self.logger.error(f"{dev.name}: POST {path} HTTP {e.code} {e.reason} — {body}")

        except urllib.error.URLError as e:
            self.logger.error(f"{dev.name}: POST {path} — cannot reach host: {e.reason}")

        except (TimeoutError, socket.timeout):
            self.logger.error(f"{dev.name}: POST {path} timed out")
            self._tokens.pop(dev.id, None)

        except Exception:
            self.logger.exception(f"{dev.name}: unexpected error on POST {path}")

        return None

    # ------------------------------------------------------------------
    # API: generic GET (per device)
    # ------------------------------------------------------------------

    def _api_get(self, dev, path, _retrying=False):
        if not self._ensure_token(dev):
            return None

        props = dev.pluginProps
        host  = props.get('host', '').strip()
        port  = props.get('port', '3052').strip()
        url   = f"http://{host}:{port}{path}"
        token = self._tokens[dev.id]['token']

        self.logger.debug(f"{dev.name}: GET {url}")

        req = urllib.request.Request(
            url,
            headers = {'Authorization': token, 'Accept': 'application/json'}
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                self.logger.debug(f"{dev.name}: GET {path} response:\n{json.dumps(data, indent=2)}")
                return data

        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not _retrying:
                self.logger.debug(f"{dev.name}: HTTP {e.code} on GET {path} — re-authenticating")
                self._tokens.pop(dev.id, None)
                if self._authenticate(dev):
                    return self._api_get(dev, path, _retrying=True)
            body = ''
            try:
                body = e.read().decode('utf-8')
            except Exception:
                pass
            self.logger.error(f"{dev.name}: GET {path} HTTP {e.code} {e.reason} — {body}")

        except urllib.error.URLError as e:
            self.logger.error(f"{dev.name}: GET {path} — cannot reach host: {e.reason}")

        except (TimeoutError, socket.timeout):
            self.logger.error(f"{dev.name}: GET {path} timed out")
            self._tokens.pop(dev.id, None)

        except Exception:
            self.logger.exception(f"{dev.name}: unexpected error on GET {path}")

        return None

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def alarmTest(self, action):
        dev    = indigo.devices[action.deviceId]
        result = self._api_post(dev, '/local/rest/v1/diagnostic/alarm_test', {})
        if result is not None:
            self.logger.info(f"{dev.name}: alarm test triggered")
        else:
            self.logger.error(f"{dev.name}: alarm test failed — check debug log")

    # ------------------------------------------------------------------
    # API: status fetch (per device)
    # ------------------------------------------------------------------

    def _get_ups_status(self, dev, _retrying=False):
        if not self._ensure_token(dev):
            return None

        props = dev.pluginProps
        host  = props.get('host', '').strip()
        port  = props.get('port', '3052').strip()
        url   = f"http://{host}:{port}/local/rest/v1/ups/status"

        # Token already includes "Bearer " prefix as returned by the login endpoint
        token = self._tokens[dev.id]['token']

        self.logger.debug(f"{dev.name}: fetching status from {url}")

        req = urllib.request.Request(
            url,
            headers = {
                'Authorization': token,
                'Accept'       : 'application/json'
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                self.logger.debug(f"{dev.name}: raw API response:\n{json.dumps(data, indent=2)}")
                return data

        except urllib.error.HTTPError as e:
            # 401 = token expired/invalid; 403 = stale session after server restart
            if e.code in (401, 403) and not _retrying:
                self.logger.debug(f"{dev.name}: HTTP {e.code} — dropping token and re-authenticating")
                self._tokens.pop(dev.id, None)
                if self._authenticate(dev):
                    return self._get_ups_status(dev, _retrying=True)
            body = ''
            try:
                body = e.read().decode('utf-8')
            except Exception:
                pass
            self.logger.error(f"{dev.name}: status HTTP {e.code} {e.reason} — {body}")

        except urllib.error.URLError as e:
            self.logger.error(f"{dev.name}: cannot reach host — {e.reason}")

        except (TimeoutError, socket.timeout):
            self.logger.error(f"{dev.name}: status request timed out — host unreachable or restarting")
            self._tokens.pop(dev.id, None)  # drop token; server may have restarted

        except Exception:
            self.logger.exception(f"{dev.name}: unexpected error fetching status")

        return None

    # ------------------------------------------------------------------
    # Device update
    # ------------------------------------------------------------------

    def _poll_all_devices(self):
        for dev in indigo.devices.iter("self"):
            if dev.enabled:
                self._update_device(dev)

    @staticmethod
    def _first(lst):
        return lst[0] if lst else None

    @staticmethod
    def _num(raw):
        """Extract leading float from strings like '244.0 V', '34 %', '750 Watts'. Returns None if blank/None."""
        if raw is None:
            return None
        try:
            return float(str(raw).split()[0])
        except (ValueError, IndexError):
            return None

    def _safe_update_states(self, dev, states):
        """
        Write states to device, dynamically registering any new keys first via
        _register_new_states() → getDeviceStateList() → Indigo state registration.
        """
        known    = set(dev.states.keys())
        incoming = {s['key'] for s in states}
        new_keys = incoming - known

        if new_keys:
            new_dicts = [s for s in states if s['key'] in new_keys]
            dev = self._register_new_states(dev, new_dicts)
            if dev is None:
                self.logger.error("Device lost during dynamic state registration — skipping update")
                return
            known = set(dev.states.keys())

        valid   = [s for s in states if s['key'] in known]
        skipped = incoming - known
        if skipped:
            self.logger.debug(f"{dev.name}: skipping states with no value yet: {skipped}")

        if valid:
            dev.updateStatesOnServer(valid)

    def _build_states(self, data):
        """
        Walk the full API response and build a state list, skipping any field
        whose value is null/None so stale Indigo state values are preserved.
        """
        states = []

        def add(key, value, ui=None, dp=None):
            if value is None:
                return
            s = {'key': key, 'value': value}
            if ui is not None:
                s['uiValue'] = ui
            if dp is not None:
                s['decimalPlaces'] = dp
            states.append(s)

        def num_state(key, raw, dp=1):
            n = self._num(raw)
            if n is not None:
                add(key, n, ui=str(raw).strip(), dp=dp)

        def list_num_state(key, lst, dp=1):
            num_state(key, self._first(lst), dp=dp)

        # --- Top level ---
        add('communicationStatus', bool(data.get('communicationAvaiable', False)))
        add('deviceId',            int(data.get('deviceId') or 0))

        # --- Input ---
        inp = data.get('input') or {}
        add('inputAvailable',  bool(inp.get('available', False)))
        add('inputState',      int(inp.get('state') or 0))
        add('inputStateText',  inp.get('stateText'))
        list_num_state('inputVoltage',     inp.get('voltages'),     dp=1)
        list_num_state('inputFrequency',   inp.get('frequencies'),  dp=2)
        list_num_state('inputCurrent',     inp.get('currents'),     dp=1)
        list_num_state('inputPowerFactor', inp.get('powerFactors'), dp=2)

        # --- Output ---
        out = data.get('output') or {}
        add('outputAvailable',  bool(out.get('available', False)))
        add('outputState',      int(out.get('state') or 0))
        add('outputStateText',  out.get('stateText'))
        list_num_state('outputVoltage',       out.get('voltages'),       dp=1)
        list_num_state('outputCurrent',       out.get('currents'),       dp=1)
        list_num_state('outputFrequency',     out.get('frequencies'),    dp=2)
        list_num_state('outputLoadPercent',   out.get('loads'),          dp=0)
        list_num_state('outputWatts',         out.get('watts'),          dp=0)
        list_num_state('outputVA',            out.get('vas'),            dp=0)
        list_num_state('outputActivePower',   out.get('activePowers'),   dp=0)
        list_num_state('outputReactivePower', out.get('reactivePowers'), dp=0)
        # NCL outlet banks — first bank only (covers nearly all single-UPS setups)
        ncls = out.get('ncls') or []
        if ncls:
            add('outputNcl1StateText', ncls[0].get('stateText'))

        # --- Battery ---
        bat = data.get('battery') or {}
        add('batteryAvailable',              bool(bat.get('available', False)))
        add('batteryState',                  int(bat.get('state') or 0))
        add('batteryStateText',              bat.get('stateText'))
        num_state('batteryVoltage',          bat.get('voltage'),   dp=1)
        num_state('batteryCapacityPercent',  bat.get('capacity'),  dp=0)
        rt = bat.get('remainingRunTimeInSecs')
        add('batteryRemainingRunTimeSecs',      None if rt is None else int(rt))
        add('batteryRemainingRunTimeFormatted', bat.get('remainingRunTimeFormated'))
        add('batteryRuntimeInsufficient',    bool(bat.get('remainingRuntimeInsufficient', False)))
        add('batteryModularRuntimeZero',     bool(bat.get('modularUpsRuntimeZero', False)))
        bhi = bat.get('bhi')
        add('batteryHealthIndex',            None if bhi is None else int(bhi))
        ct = bat.get('chargeTimeInSecs')
        add('batteryChargeTimeSecs',         None if ct is None else int(ct))
        add('batteryChargeTimeFormatted',    bat.get('chargeTimeFormated'))
        add('batteryTemperatureCelsius',     bat.get('temperatureCelsius'), dp=1)
        num_state('batteryHighVoltage',      bat.get('highVoltage'), dp=1)
        num_state('batteryLowVoltage',       bat.get('lowVoltage'),  dp=1)

        # --- Bypass ---
        byp = data.get('bypass') or {}
        add('bypassAvailable',  bool(byp.get('available', False)))
        bs = byp.get('state')
        add('bypassState',      None if bs is None else int(bs))
        add('bypassStateText',  byp.get('stateText'))
        list_num_state('bypassVoltage',   byp.get('voltages'),   dp=1)
        list_num_state('bypassFrequency', byp.get('frequencies'), dp=2)

        # --- System ---
        sys_data = data.get('system') or {}
        add('systemAvailable',             bool(sys_data.get('available', False)))
        add('systemState',                 int(sys_data.get('state') or 0))
        add('systemStateText',             sys_data.get('stateText'))
        add('systemHardwareFaultAvailable', bool(sys_data.get('hardwareFaultAvailable', False)))
        add('systemHardwareFaultCode',     sys_data.get('originalHardwareFaultCode'))
        add('systemTemperatureCelsius',    sys_data.get('temperatureCelsius'), dp=1)
        add('systemTemperatureFormatted',  sys_data.get('temperatureFormated'))

        # --- Meta ---
        add('lastUpdate', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        return states

    def _build_spec_states(self, data):
        """Build states from /ups/spec — UPS identity and rating data."""
        states = []

        def add(key, value):
            if value is None or value == '':
                return
            states.append({'key': key, 'value': value})

        add('specUpsModel',            data.get('upsModel'))
        add('specSerialNo',            data.get('serialNo'))
        add('specUpsType',             data.get('upsType'))
        add('specRatingPower',         data.get('ratingPower'))
        add('specRatingVolt',          data.get('ratingVolt'))
        add('specRatingFreq',          data.get('ratingFreq'))
        add('specMaxCurrent',          data.get('maxCurrent'))
        add('specHardwareVersion',     data.get('hardwareVersion'))
        add('specUsbVersion',          data.get('usbVersion'))
        add('specDeviceName',          data.get('deviveName'))       # API typo: deviveName
        add('specLocation',            data.get('location'))
        add('specContact',             data.get('contact'))
        add('specBatteryReplacedDate', data.get('replacedDate'))
        add('specNextReplacementDate', data.get('nextReplacementDate'))
        add('specIsExpired',           bool(data.get('isExpried',             False)))  # API typo: isExpried
        add('specIsThreePhase',        bool(data.get('isThreePhase',          False)))
        add('specEbmSupported',        bool(data.get('ebmSupported',          False)))
        add('specIndividualBankControl', bool(data.get('individualBankControlUps', False)))

        alone = data.get('aloneOutletNumber')
        if alone is not None:
            try:
                add('specAloneOutletNumber', int(alone))
            except (ValueError, TypeError):
                pass

        ebm = data.get('externalBatteryCabinetNumber')
        if ebm is not None:
            try:
                add('specExternalBatteryCabinets', int(ebm))
            except (ValueError, TypeError):
                pass

        return states

    def _build_summary_states(self, data):
        """Build states from /ups/summary — normal and warning message lists."""
        normal  = data.get('summaryNormalMsg')  or []
        warning = data.get('summaryWarningMsg') or []
        return [
            {'key': 'summaryNormalMessage',  'value': ' '.join(normal)},
            {'key': 'summaryWarningMessage', 'value': ' '.join(warning)},
            {'key': 'summaryHasWarning',     'value': bool(warning)},
        ]

    def _update_device(self, dev):
        data = self._get_ups_status(dev)

        if data is None:
            dev.setErrorStateOnServer("Connection failed")
            dev.updateStateImageOnServer(indigo.kStateImageSel.Error)
            return

        states = self._build_states(data)

        time.sleep(0.3)
        spec_data = self._api_get(dev, '/local/rest/v1/ups/spec')
        if spec_data:
            states.extend(self._build_spec_states(spec_data))

        time.sleep(0.3)
        summary_data = self._api_get(dev, '/local/rest/v1/ups/summary')
        if summary_data:
            states.extend(self._build_summary_states(summary_data))

        if self.debug:
            lines = [
                f"  {s['key']:<44} = {s['value']!r}" + (f"  (ui: {s['uiValue']!r})" if 'uiValue' in s else '')
                for s in states
            ]
            self.logger.debug(f"{dev.name}: states to write ({len(states)}):\n" + "\n".join(lines))

        self._safe_update_states(dev, states)
        dev.setErrorStateOnServer(None)

        sys_state = (data.get('system') or {}).get('state', 0)
        if sys_state == 0:
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
        else:
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
