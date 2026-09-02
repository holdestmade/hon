import asyncio
import logging
import secrets
import hashlib
import base64
import json
import time
from datetime import datetime, timezone
from homeassistant.helpers.aiohttp_client import async_create_clientsession

_LOGGER = logging.getLogger(__name__)

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from homeassistant.helpers import device_registry as dr


from .const import (
    CONF_ID_TOKEN,
    CONF_COGNITO_TOKEN,
    CONF_REFRESH_TOKEN,
    API_URL,
    APP_VERSION,
    OS_VERSION,
    OS,
    DEVICE_MODEL,
)

# CIAM access tokens expire after ~15 minutes, so refresh well before that.
SESSION_TIMEOUT     = 600 # seconds

# Back-off between failed logins. Without it a refusal from the cloud turns into
# one full credential submission per coordinator poll (every 60s, indefinitely),
# which is both useless and a good way to get an account rate-limited.
AUTH_RETRY_BACKOFF  = (60, 120, 300, 600, 900) # seconds

from .base import HonBaseCoordinator



class HonConnection:
    def __init__(self, hass, entry, email = None, password = None) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator_dict  = {}
        self._mobile_id = secrets.token_hex(8)

        # Only used during registration (Login/password check)
        if( email != None ) and ( password != None ):
            self._email = email
            self._password = password
        else:
            self._email = entry.data[CONF_EMAIL]
            self._password = entry.data[CONF_PASSWORD]
            self._id_token = entry.data.get(CONF_ID_TOKEN, "")
            self._refresh_token = entry.data.get(CONF_REFRESH_TOKEN, "")
            self._cognitoToken = entry.data.get(CONF_COGNITO_TOKEN, "")

        self._start_time        = time.time()
        self._auth_lock         = asyncio.Lock()
        self._auth_failures     = 0
        self._next_auth_attempt = 0.0

        self._header = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        }
        # Use Home Assistant's client session helper: it keeps TLS verification
        # on (credentials and tokens travel over this connection) and hooks the
        # session into HA's own connection pooling and shutdown handling.
        self._session = async_create_clientsession(hass, headers=self._header)
        self._appliances = []

    async def async_close(self):
        await self._session.close()

    @property
    def appliances(self):
        return self._appliances

    @property
    def coordinators(self):
        """Coordinators owned by this connection, keyed by MAC address."""
        return self._coordinator_dict

    async def async_get_existing_coordinator(self, mac):
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac]
        return None

    async def async_get_coordinator(self, appliance):
        mac = appliance.get("macAddress", "")
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac]
        coordinator = HonBaseCoordinator(self._hass, self, appliance)
        self._coordinator_dict[mac] = coordinator
        return coordinator


    async def _ensure_session(self):
        """Refresh the CIAM tokens when they are close to expiring (~15 min TTL).

        Only the tokens are renewed here. The appliance list is loaded once at
        setup: folding it into the refresh meant any failure reading it -- a
        cloud-side outage, say -- marked the whole session invalid, so the next
        poll logged in again, and so did every poll after that.

        Every coordinator polls on its own schedule, so without the lock a
        multi-appliance account fires one login per appliance the moment the
        token ages out. The second check inside the lock lets the tasks that
        queued behind the winner reuse the tokens it just fetched.
        """
        if time.time() - self._start_time <= SESSION_TIMEOUT:
            return True

        async with self._auth_lock:
            now = time.time()
            if now - self._start_time <= SESSION_TIMEOUT:
                return True
            if now < self._next_auth_attempt:
                return False
            return await self._async_login()

    def _auth_failed(self):
        """Record a failed login and schedule the next attempt."""
        delay = AUTH_RETRY_BACKOFF[min(self._auth_failures, len(AUTH_RETRY_BACKOFF) - 1)]
        self._auth_failures += 1
        self._next_auth_attempt = time.time() + delay
        _LOGGER.debug("Login failed %s time(s), next attempt in %ss",
                      self._auth_failures, delay)
        return False

    async def async_authorize(self):
        """Log in and load the appliance list. Used at setup and reconfiguration."""
        if not await self._async_login():
            return False
        return await self.async_load_appliances()

    async def _async_login(self):
        """Obtain CIAM tokens.

        Replaces the legacy Salesforce Aura / OAuth2 login that Haier retired in
        2026-06: the app now logs in through /ciam/authorize + /ciam/token (PKCE).
        """
        # PKCE (S256) verifier + challenge
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        # 1) Submit credentials, receive a one-time session id
        params = {
            "username": self._email,
            "password": self._password,
            "code_challenge": code_challenge,
        }
        async with self._session.get(f"{API_URL}/ciam/authorize", params=params) as resp:
            if resp.status != 200:
                _LOGGER.error("Unable to connect to the CIAM authorize service: %s", resp.status)
                return self._auth_failed()
            session_id = (await resp.json()).get("session_id")
            if not session_id:
                _LOGGER.error("Unable to get [session_id] - check your email/password")
                return self._auth_failed()

        # 2) Exchange the session id (+ PKCE verifier) for the tokens
        async with self._session.post(
            f"{API_URL}/ciam/token",
            json={"session_id": session_id, "code_verifier": code_verifier},
        ) as resp:
            try:
                tokens = (await resp.json())["tokens"]
                self._cognitoToken = tokens["cognito_token"]
                self._id_token = tokens["id_token"]
                self._refresh_token = tokens.get("refresh_token", "")
            except (KeyError, TypeError):
                _LOGGER.error("Unable to get tokens from /ciam/token. Response: %s", await resp.text())
                return self._auth_failed()

        self._start_time = time.time()
        self._auth_failures = 0
        self._next_auth_attempt = 0.0
        return True

    async def async_load_appliances(self):
        """Read the account's appliances from the unified-api view.

        Called once per setup: the old /commands/v1/appliance endpoint now
        returns an empty list.
        """
        url = f"{API_URL}/unified-api/v1/view/appliance-list"
        async with self._session.post(url, headers=self._headers, json={"deviceId": "homeassistant"}) as resp:
            try:
                json_data = await resp.json(content_type=None)
            except ValueError:  # not JSON at all (gateway error page, empty body)
                _LOGGER.error("hOn appliance list: unreadable response [%s] from [%s]",
                              (await resp.text())[:300], url)
                return False

            module = (json_data or {}).get("modules", {}).get("applianceList", {})
            appliances = module.get("payload", {}).get("appliances")

            if appliances is None:
                # The cloud answers 200 with the failure nested in the module, so
                # surface its own message rather than dumping the raw envelope.
                _LOGGER.error(
                    "hOn appliance list unavailable (HTTP %s): %s [%s]. This is "
                    "reported by the hOn cloud, not by Home Assistant; if it "
                    "persists check https://github.com/gvigroux/hon/issues",
                    resp.status,
                    module.get("message") or (await resp.text())[:300],
                    module.get("code", "unknown"),
                )
                return False

            _LOGGER.debug("All appliances: %s", appliances)

            # Keep only appliances carrying the fields every entity needs to
            # identify itself; a partial payload would otherwise take the whole
            # account down when the coordinator is built.
            required = ("macAddress", "applianceTypeId", "applianceTypeName")
            usable, skipped = [], []
            for appliance in appliances:
                (usable if all(f in appliance for f in required) else skipped).append(appliance)
            if skipped:
                _LOGGER.warning("Ignoring %s appliance(s) with incomplete data: %s",
                                len(skipped), skipped)
            self._appliances = usable

        return True


    async def load_commands(self, appliance):
        await self._ensure_session()

        params = {
            "applianceType": appliance["applianceTypeId"],
            "code": appliance["code"],
            "applianceModelId": appliance["applianceModelId"],
            "firmwareId": appliance["eepromId"],
            "macAddress": appliance["macAddress"],
            "fwVersion": appliance["fwVersion"],
            "os": OS,
            "appVersion": APP_VERSION,
            "series": appliance["series"],
        }
        url = f"{API_URL}/commands/v1/retrieve"
        async with self._session.get(url, params=params, headers=self._headers) as resp:
            result = (await resp.json()).get("payload", {})
            if not result or result.pop("resultCode") != "0":
                return {}
            _LOGGER.debug(f"Commands: {result}")
            return result

    async def async_get_context(self, device):

        # Refresh the CIAM session before it expires
        await self._ensure_session()

        params = {
            "macAddress": device.mac_address,
            "applianceType": device.appliance_type,
            "category": "CYCLE"
        }
        url = f"{API_URL}/commands/v1/context"
        async with self._session.get(url, params=params, headers=self._headers) as response:
            data = await response.json()
            _LOGGER.debug(f"Context for mac[{device.mac_address}] type [{device.appliance_type}] {data}")
            return data.get("payload", {})

    async def load_statistics(self, device):
        await self._ensure_session()

        params = {
            "macAddress": device.mac_address,
            "applianceType": device.appliance_type
        }
        url = f"{API_URL}/commands/v1/statistics"
        async with self._session.get(url, params=params, headers=self._headers) as response:
            data = await response.json()
            _LOGGER.debug(f"Statistic for mac[{device.mac_address}] type [{device.appliance_type}] {data}")
            return data.get("payload", {})

    @property
    def _headers(self):
        return {
            "Content-Type": "application/json",
            "cognito-token": self._cognitoToken,
            "id-token": self._id_token,
        }

    async def async_set(self, mac, typeName, parameters):

        await self._ensure_session()

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        command = json.loads("{}")
        command["macAddress"] = mac
        command["commandName"] = "startProgram"
        command["applianceOptions"] = json.loads("{}")
        command["programName"] = "PROGRAMS." + typeName + ".HOME_ASSISTANT"
        command["ancillaryParameters"] = json.loads(
            '{"programFamily":"[standard]", "remoteActionable": "1", "remoteVisible": "1"}'
        )
        command["applianceType"] = typeName
        command["attributes"] = json.loads(
            '{"prStr":"HOME_ASSISTANT", "channel":"googleHome", "origin": "conversationalVoice"}'
        )
        if typeName == "WM":
            command["attributes"] = json.loads(
            '{"prStr":"HOME_ASSISTANT", "channel":"googleHome", "origin": "conversationalVoice", "energyLabel": "0"}'
        )
        command["device"] = json.loads(
            '{"mobileId":"xxxxxxxxxxxxxxxxxxx", "mobileOs": "android", "osVersion": "31", "appVersion": "1.53.4", "deviceModel": "lito"}'
        )
        command["parameters"] = parameters
        command["timestamp"] = timestamp
        command["transactionId"] = mac + "_" + command["timestamp"]
        _LOGGER.debug((f"Command sent (async_set): {command}"))

        async with self._session.post(f"{API_URL}/commands/v1/send",headers=self._headers,json=command,) as resp:
            try:
                data = await resp.json()
                _LOGGER.debug((f"Command result (async_set): {data}"))
            except json.JSONDecodeError:
                _LOGGER.error("hOn Invalid Data ["+ str(resp.text()) + "] after sending command ["+ str(command)+ "]")
                return False
            if data["payload"]["resultCode"] == "0":
                return True
            _LOGGER.error("hOn command has been rejected. Error message ["+ str(data) + "] sent command ["+ str(command)+ "]")
        return False


    async def send_command(self, device, command, parameters, ancillary_parameters):

        await self._ensure_session()

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        command = {
            "macAddress": device.mac_address,
            "timestamp": f"{now[:-3]}Z",
            "commandName": command,
            "transactionId": f"{device.mac_address}_{now[:-3]}Z",
            "applianceOptions": device.commands_options,
            "device": {
                "mobileId": self._mobile_id,
                "mobileOs": OS,
                "osVersion": OS_VERSION,
                "appVersion": APP_VERSION,
                "deviceModel": DEVICE_MODEL
            },
            "attributes": {
                "channel": "mobileApp",
                "origin": "standardProgram",
                "energyLabel": "0"
            },
            "ancillaryParameters": ancillary_parameters,
            "parameters": parameters,
            "applianceType": device.appliance_type
        }
        _LOGGER.debug((f"Command sent (send_command): {command}"))

        url = f"{API_URL}/commands/v1/send"
        async with self._session.post(url, headers=self._headers, json=command) as resp:
            try:
                data = await resp.json()
                _LOGGER.debug((f"Command result (send_command): {data}"))
            except json.JSONDecodeError:
                _LOGGER.error("hOn Invalid Data ["+ str(resp.text()) + "] after sending command ["+ str(command)+ "]")
                return False
            if data["payload"]["resultCode"] == "0":
                return True
            _LOGGER.error("hOn command has been rejected. Error message ["+ str(data) + "] sent data ["+ str(command)+ "]")
        return False

    def get_device(self, hass, device_id):
        mac = get_hOn_mac(device_id, hass)
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac].device
        _LOGGER.error(f"Unable to find the device with ID: {device_id} and mac: {mac}")
        return None

def get_hOn_mac(device_id, hass):
    """Return the MAC address the device registry holds for `device_id`."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not device.identifiers:
        _LOGGER.error("Unknown device_id: %s", device_id)
        return None
    return next(iter(device.identifiers))[1]
