import logging
import voluptuous as vol
import ast

from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import config_validation as cv
from homeassistant.core import HomeAssistant, ServiceCall

from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import HomeAssistantError, ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PLATFORMS
from .hon import HonConnection, get_hOn_mac
from .device import HonDevice


_LOGGER = logging.getLogger(__name__)
SERVICE_REGISTRY = "service_registry"


def _connections(hass):
    """Every configured hOn connection, one per config entry."""
    return [
        value
        for key, value in hass.data.get(DOMAIN, {}).items()
        if key != SERVICE_REGISTRY
    ]


def _connection_for_device(hass, device_id):
    """Return the connection that owns `device_id`.

    Services are registered once for the whole domain, so a call has to be routed
    to the account the target device actually belongs to rather than to whichever
    config entry happened to load first.
    """
    mac = get_hOn_mac(device_id, hass)
    if mac is not None:
        for hon in _connections(hass):
            if mac in hon.coordinators:
                return hon, mac
    raise HomeAssistantError(f"No hOn device found for device_id [{device_id}]")


def _device_for_id(hass, device_id):
    hon, _mac = _connection_for_device(hass, device_id)
    device = hon.get_device(hass, device_id)
    if device is None:
        raise HomeAssistantError(f"No hOn device found for device_id [{device_id}]")
    return device


async def _async_set_for_device(hass, device_id, parameters):
    """Send parameters to one device and refresh it afterwards."""
    hon, mac = _connection_for_device(hass, device_id)
    coordinator = await hon.async_get_existing_coordinator(mac)
    await coordinator.async_set(parameters)
    await coordinator.async_request_refresh()


HON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema(vol.All(cv.ensure_list, [HON_SCHEMA]))},
    extra=vol.ALLOW_EXTRA,
)

#TODO merge all programes names in language file

# This method will update a sensor value with the targetted one for a better user experience
def update_sensor(hass, device_id, mac, sensor_name, state):

    entity_reg  = er.async_get(hass)
    entries     = er.async_entries_for_device(entity_reg, device_id)

    # Loop over all entries and update the good one
    for entry in entries:
        if( entry.unique_id == mac + '_' + sensor_name):
            inputStateObject = hass.states.get(entry.entity_id)
            hass.states.async_set(entry.entity_id, state, inputStateObject.attributes)

def get_parameters(call):
    parameters_str = call.data.get("parameters", "{}")
    if type(parameters_str) != str:
        parameters_str = str(parameters_str)
    return ast.literal_eval(parameters_str)


def _minutes_until(target: datetime, now: datetime) -> int:
    """Return the number of whole minutes until the target time."""
    return max(0, int((target - now).total_seconds() / 60))

def get_device_ids(hass, call):
    device_ids = set(call.data.get("device_id", []))
    entity_ids = call.data.get("entity_id", [])

    ent_reg = er.async_get(hass)

    for entity_id in entity_ids:
        entry = ent_reg.async_get(entity_id)
        if entry and entry.device_id:
            device_ids.add(entry.device_id)

    return list(device_ids)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    hon = HonConnection(hass, entry)
    try:
        result = await hon.async_authorize()
    except Exception as e:
        raise ConfigEntryNotReady(f"hOn connection failed: {e}") from e
    if not result:
        raise ConfigEntryNotReady("hOn authentication failed")

    # Log all appliances
    _LOGGER.debug("Appliances: %s", hon.appliances)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = hon
    hass.data[DOMAIN].setdefault(SERVICE_REGISTRY, set())

    for appliance in hon.appliances:
        
        coordinator = await hon.async_get_coordinator(appliance)
        coordinator.device = HonDevice(hon, coordinator, appliance)
        await coordinator.async_config_entry_first_refresh()

        await coordinator.device.load_commands()
        await coordinator.device.load_statistics()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


    async def handle_oven_start(call):

        delay_time = 0
        tz = dt_util.DEFAULT_TIME_ZONE

        if "start" in call.data:
            date = datetime.strptime(call.data.get("start"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            delay_time = _minutes_until(date, dt_util.now())

        if "end" in call.data and "duration" in call.data:
            date = datetime.strptime(call.data.get("end"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            duration = call.data.get("duration")
            delay_time = max(0, _minutes_until(date, dt_util.now()) - duration)

        parameters = {
            "delayTime": delay_time,
            "onOffStatus": "1",
            "prCode": call.data.get("program"),
            "prPosition": "1",
            "recipeId": "NULL",
            "recipeStep": "1",
            "prTime": call.data.get("duration", "0"),
            "tempSel": call.data.get("temperature"),
            "preheatStatus": "1" if call.data.get("preheat", False) else "0",
        }

        hon, mac = _connection_for_device(hass, call.data.get("device"))
        return await hon.async_set(mac, "OV", parameters)

    
    async def handle_dishwasher_start(call):

        delay_time = 0
        tz = dt_util.DEFAULT_TIME_ZONE

        if "start" in call.data:
            date = datetime.strptime(call.data.get("start"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            delay_time = _minutes_until(date, dt_util.now())

        if "end" in call.data and "duration" in call.data:
            date = datetime.strptime(call.data.get("end"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            duration = call.data.get("duration")
            delay_time = max(0, _minutes_until(date, dt_util.now()) - duration)

        parameters = {
            "delayTime": delay_time,
            "onOffStatus": "1",
            "prCode": call.data.get("program"),
            "prPosition": "1",
            "prTime": call.data.get("duration", "0"),
 #           "extraDry": "1" if call.data.get("extra_dry", False) else "0",
 #           "openDoor": "1" if call.data.get("open_door", False) else "0", ##conditional program
 #           "halfLoad": "1" if call.data.get("half_load", False) else "0", ##conditional programm
 #           "prStrDisp": call.data.get("string_display"),
        }

        hon, mac = _connection_for_device(hass, call.data.get("device"))
        return await hon.async_set(mac, "DW", parameters)
    
    async def handle_washingmachine_start(call):

        delay_time = 0
        tz = dt_util.DEFAULT_TIME_ZONE
        if "end" in call.data:
            date = datetime.strptime(call.data.get("end"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            delay_time = _minutes_until(date, dt_util.now())

        parameters = {
                    "haier_MainWashSpeed": "50",
                    "creaseResistSoakStatus": "0",
                    "haier_SoakPrewashSelection": "0",
                    "prCode": "999",
                    "soakWashStatus": "0",
                    "strongStatus": "0",
                    "energySavingStatus": "0",
                    "spinSpeed": call.data.get("spinSpeed", "400"),
                    "haier_MainWashWaterLevel": "2",
                    "rinseIterationTime": "8",
                    "haier_SoakPrewashSpeed": "0",
                    "permanentPressStatus": "1",
                    "nightWashStatus": "0",
                    "intelligenceStatus": "0",
                    "haier_SoakPrewashStopTime": "0",
                    "weight": "5",
                    "highWaterLevelStatus": "0",
                    "voiceStatus": "0",
                    "haier_SoakPrewashTime": "0",
                    "autoDisinfectantStatus": "0",
                    "cloudProgSrc": "2",
                    "haier_SoakPrewashRotateTime": "0",
                    "cloudProgId": "255",
                    "haier_SoakPrewashTemperature": "0",
                    "dryProgFlag": "0",
                    "dryLevel": "0",
                    "haier_RinseRotateTime": "20",
                    "uvSterilizationStatus": "0",
                    "dryTime": "0",
                    "delayStatus": "0",
                    "dryLevelAllowed": "0",
                    "rinseIterations": call.data.get("rinseIterations", "2"),
                    "lockStatus": "0",
                    "mainWashTime": call.data.get("mainWashTime", "15"),
                    "autoSoftenerStatus": call.data.get("autoSoftenerStatus", "0"),
                    "washerDryIntensity": "1",
                    "autoDetergentStatus": "0",
                    "antiAllergyStatus": "0",
                    "speedUpStatus": "0",
                    "temp": call.data.get("temp", "30"),
                    "haier_MainWashRotateTime": "20",
                    "detergentBStatus": "0",
                    "haier_MainWashStopTime": "5",
                    "texture": "1",
                    "operationName": "grOnlineWash",
                    "haier_RinseSpeed": "50",
                    "haier_ConstantTempStatus": "1",
                    "haier_RinseStopTime": "5",
                    "delayTime": delay_time
                }

        device_id = call.data.get("device")
        hon, mac = _connection_for_device(hass, device_id)

        # The CIAM migration removed the standalone state endpoint; the last
        # connection event from the polled context carries the same information.
        device = hon.get_device(hass, device_id)
        if device is not None and device.get("attributes.lastConnEvent.category") == "DISCONNECTED":
            _LOGGER.error("This hOn device is disconnected - Mac address [%s]", mac)
            return False

        return await hon.async_set(mac, "WM", parameters)


    async def _async_purifier_set(call, parameters):
        hon, mac = _connection_for_device(hass, call.data.get("device"))
        return await hon.async_set(mac, "AP", parameters)

    async def handle_oven_stop(call):
        hon, mac = _connection_for_device(hass, call.data.get("device"))
        return await hon.async_set(mac, "OV", {"onOffStatus": "0"})

    async def handle_washingmachine_stop(call):
        hon, mac = _connection_for_device(hass, call.data.get("device"))
        return await hon.async_set(mac, "WM", {"onOffStatus": "0", "machMode": "1"})

    async def handle_purifier_start(call):
        return await _async_purifier_set(call, {"onOffStatus": "1", "machMode": "2"})

    async def handle_purifier_stop(call):
        return await _async_purifier_set(call, {"onOffStatus": "0", "machMode": "1"})

    async def handle_purifier_maxmode(call):
        return await _async_purifier_set(call, {"machMode": "4"})

    async def handle_purifier_automode(call):
        return await _async_purifier_set(call, {"machMode": "2"})

    async def handle_purifier_sleepmode(call):
        return await _async_purifier_set(call, {"machMode": "1"})


    # Generic method to set a mode to any hOn device
    async def handle_set_mode(call):
        parameters = {"onOffStatus": "1", "machMode": call.data.get("mode", 1)}
        await _async_set_for_device(hass, call.data.get("device"), parameters)

    # Generic method to TURN OFF any hOn device
    async def handle_turn_off(call):
        parameters = {"onOffStatus": "0", "machMode": "1"}
        await _async_set_for_device(hass, call.data.get("device"), parameters)

    async def handle_light_on(call):
        device_id = call.data.get("device")
        _hon, mac = _connection_for_device(hass, device_id)
        update_sensor(hass, device_id, mac, "light_status", "on")
        await _async_set_for_device(hass, device_id, {"lightStatus": "1"})

    async def handle_light_off(call):
        device_id = call.data.get("device")
        _hon, mac = _connection_for_device(hass, device_id)
        update_sensor(hass, device_id, mac, "light_status", "off")
        await _async_set_for_device(hass, device_id, {"lightStatus": "0"})

    async def handle_health_mode_on(call):
        device_id = call.data.get("device")
        _hon, mac = _connection_for_device(hass, device_id)
        update_sensor(hass, device_id, mac, "health_mode", "on")
        await _async_set_for_device(hass, device_id, {"healthMode": "1"})

    async def handle_health_mode_off(call):
        device_id = call.data.get("device")
        _hon, mac = _connection_for_device(hass, device_id)
        update_sensor(hass, device_id, mac, "health_mode", "off")
        await _async_set_for_device(hass, device_id, {"healthMode": "0"})
    

    async def handle_start_program(call):
        parameters = get_parameters(call)
        program = call.data.get("program")

        for device_id in get_device_ids(hass, call):
            device = _device_for_id(hass, device_id)
            command = device.commands.get("startProgram")
            if command is None:
                raise HomeAssistantError(
                    f"Device [{device.name}] has no startProgram command"
                )
            programs = command.get_programs()
            if program not in programs:
                keys = ", ".join(programs)
                raise HomeAssistantError(f"Invalid [Program] value, allowed values [{keys}]")

            await device.start_command(program, parameters).send()

    async def handle_custom_request(call):
        parameters = get_parameters(call)
        for device_id in get_device_ids(hass, call):
            await _async_set_for_device(hass, device_id, parameters)

    async def handle_update_settings(call):
        parameters = get_parameters(call)
        for device_id in get_device_ids(hass, call):
            device = _device_for_id(hass, device_id)
            await device.settings_command(parameters).send()

    async def async_get_setting(call: ServiceCall):
        """Handle the get_setting service call."""
        parameter = call.data.get("parameter")

        results = {}
        for device_id in get_device_ids(hass, call):
            results[device_id] = _device_for_id(hass, device_id).get(parameter)

        _LOGGER.debug("get_setting results: %s", results)
        hass.bus.async_fire("hon_get_setting_result", {"results": results})
        return results



    services = {
        "turn_on_washingmachine": handle_washingmachine_start,
        "turn_on_oven": handle_oven_start,
        "turn_on_dishwasher": handle_dishwasher_start,
        "turn_on_purifier": handle_purifier_start,
        "turn_off_oven": handle_oven_stop,
        "turn_off_washingmachine": handle_washingmachine_stop,
        "turn_off_purifier": handle_purifier_stop,
        "set_auto_mode_purifier": handle_purifier_automode,
        "set_sleep_mode_purifier": handle_purifier_sleepmode,
        "set_max_mode_purifier": handle_purifier_maxmode,
        "set_mode": handle_set_mode,
        "turn_off": handle_turn_off,
        "turn_light_on": handle_light_on,
        "turn_light_off": handle_light_off,
        "send_custom_request": handle_custom_request,
        "climate_turn_health_mode_on": handle_health_mode_on,
        "climate_turn_health_mode_off": handle_health_mode_off,
        "start_program": handle_start_program,
        "update_settings": handle_update_settings,
        "get_setting": async_get_setting,
    }

    registered_services = hass.data[DOMAIN][SERVICE_REGISTRY]
    for service_name, handler in services.items():
        if service_name in registered_services:
            continue
        hass.services.async_register(
            domain=DOMAIN,
            service=service_name,
            service_func=handler,
            schema=None,
        )
        registered_services.add(service_name)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    # The connection holds no resources of its own: it borrows Home Assistant's
    # shared aiohttp session, which HA owns and closes.
    hass.data[DOMAIN].pop(entry.entry_id, None)

    remaining_entries = [
        key for key in hass.data.get(DOMAIN, {}) if key != SERVICE_REGISTRY
    ]
    if not remaining_entries:
        for service_name in hass.data[DOMAIN].get(SERVICE_REGISTRY, set()):
            hass.services.async_remove(DOMAIN, service_name)
        hass.data.pop(DOMAIN, None)

    return True
