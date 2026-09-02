import logging
import re
from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)

from .const import DOMAIN, APPLIANCE_DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)


def _set_appliance_attributes(target, appliance):
    """Copy the appliance fields every hOn object needs onto `target`.

    Only the identity fields are mandatory; brand, model and firmware are purely
    cosmetic, so a payload missing them still yields a working entity instead of
    a half-initialised object that raises AttributeError somewhere unrelated.
    """
    missing = [
        key
        for key in ("macAddress", "applianceTypeId", "applianceTypeName")
        if key not in appliance
    ]
    if missing:
        raise KeyError(f"Appliance data is missing {', '.join(missing)}: {appliance}")

    target._mac         = appliance["macAddress"]
    target._type_id     = appliance["applianceTypeId"]
    target._type_name   = appliance["applianceTypeName"]
    target._name        = appliance_name(appliance)
    target._brand       = appliance.get("brand", "Haier")
    target._model       = appliance.get("modelName", "")
    target._fw_version  = appliance.get("fwVersion", "")


def appliance_name(appliance):
    """Return the user facing name of an appliance."""
    type_id = appliance["applianceTypeId"]
    return appliance.get(
        "nickName",
        APPLIANCE_DEFAULT_NAME.get(str(type_id), f"Device ID: {type_id}"),
    )


class HonDeviceInfoMixin:
    """Shared `device_info` for everything belonging to one hOn appliance.

    The identifiers keep the historical (domain, mac, type name) shape: changing
    them would orphan every device already in the registry, taking the user's
    entity names, areas and automations with it.
    """

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._mac, self._type_name)},
            "name": self._name,
            "manufacturer": self._brand,
            "model": self._model,
            "sw_version": self._fw_version,
        }


class HonBaseCoordinator(HonDeviceInfoMixin, DataUpdateCoordinator):
    def __init__(self, hass, hon, appliance):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="hOn Device",
            update_interval=timedelta(seconds=60),
        )
        self._hon       = hon
        self._device    = None
        self._appliance = appliance

        _set_appliance_attributes(self, appliance)

    async def _async_update_data(self):
        await self._device.load_context()

    @property
    def device(self):
        return self._device

    @device.setter
    def device(self, value):
        self._device = value

    async def async_set(self, parameters):
        await self._hon.async_set(self._mac, self._type_name, parameters)

    def get(self, key):
        return (self.data or {}).get(key, "")


class HonBaseEntity(HonDeviceInfoMixin, CoordinatorEntity):
    """Common wiring for the entities built straight off an appliance dict."""

    def __init__(self, coordinator, appliance, key, sensor_name) -> None:
        super().__init__(coordinator)
        self._coordinator   = coordinator
        _set_appliance_attributes(self, appliance)
        self._key           = key
        self._device        = coordinator.device

        # Generate unique ID from key
        key_formatted = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
        if len(key_formatted) <= 0:
            key_formatted = re.sub(r'(?<!^)(?=[A-Z])', '_', sensor_name).lower()
        self._attr_unique_id = self._mac + "_" + key_formatted

        self._attr_name = self._name + " " + sensor_name
        self.coordinator_update()

    @callback
    def _handle_coordinator_update(self):
        self.coordinator_update()
        self.async_write_ha_state()

    def coordinator_update(self):
        raise NotImplementedError


class HonBaseBinarySensorEntity(HonBaseEntity, BinarySensorEntity):
    def coordinator_update(self):
        self._attr_is_on = self._device.get(self._key) == "1"


class HonBaseSensorEntity(HonBaseEntity, SensorEntity):
    def coordinator_update(self):
        self._attr_native_value = self._device.get(self._key)


class HonBaseSwitchEntity(HonBaseEntity, SwitchEntity):
    def __init__(self, coordinator, appliance, entity_description) -> None:
        self.entity_description = entity_description
        self._attr_icon         = entity_description.icon
        self.translation_key    = entity_description.translation_key
        super().__init__(
            coordinator, appliance, entity_description.key, entity_description.name
        )

    def coordinator_update(self):
        self._attr_is_on = self._device.get(self._key) == "1"
