import logging
from dataclasses import dataclass
from typing import Any


from .const import DOMAIN
from .parameter import HonParameter, HonParameterRange
from .base import HonBaseSwitchEntity


from homeassistant.core import callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.switch import SwitchEntityDescription

_LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class HonControlSwitchEntityDescription(SwitchEntityDescription):
    turn_on_key: str = ""
    turn_off_key: str = ""


@dataclass(frozen=True)
class HonSwitchEntityDescription(SwitchEntityDescription):
    pass



SWITCH_DESCRIPTIONS = (
    (HonSwitchEntityDescription(
        key="silentSleepStatus",
        name="Sleep Mode",
        icon="mdi:bed",
        translation_key="sleep_mode",
    ), False),
    (HonSwitchEntityDescription(
        key="screenDisplayStatus",
        name="Screen Display",
        icon="mdi:monitor-small",
        translation_key="screen_display_status",
    ), False),
    (HonSwitchEntityDescription(
        key="muteStatus",
        name="Silent Mode",
        icon="mdi:volume-off",
        translation_key="silent_mode",
    ), False),
    (HonSwitchEntityDescription(
        key="echoStatus",
        name="Echo",
        icon="mdi:account-voice",
        translation_key="echo_status",
    ), True),
    (HonSwitchEntityDescription(
        key="rapidMode",
        name="Rapid Mode",
        icon="mdi:car-turbocharger",
        translation_key="rapid_mode",
    ), False),
    (HonSwitchEntityDescription(
        key="10degreeHeatingStatus",
        name="10° Heating",
        icon="mdi:heat-wave",
        translation_key="10_degree_heating",
    ), False),
    (HonSwitchEntityDescription(
        key="ecoMode",
        name="Eco Mode",
        icon="mdi:sprout",
        translation_key="eco_mode",
    ), False),
    (HonSwitchEntityDescription(
        key="turboMode",
        name="Turbo Mode",
        icon="mdi:rocket-launch",
        translation_key="turbo_mode",
    ), False),
    (HonSwitchEntityDescription(
        key="healthMode",
        name="Health Mode",
        icon="mdi:heart",
        translation_key="health_mode",
    ), False),
)


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:

    hon = hass.data[DOMAIN][entry.entry_id]

    appliances = []
    for appliance in hon.appliances:
        coordinator = await hon.async_get_coordinator(appliance)
        device = coordinator.device

        if "settings" not in device.commands:
            continue

        # Each description is gated on its own key: the previous code checked
        # `ecoMode` for the turbo switch, which put a dead Turbo Mode entity on
        # every appliance that supports Eco Mode.
        for description, invert in SWITCH_DESCRIPTIONS:
            if device.get(description.key) is None:
                continue
            appliances.append(
                HonSwitchEntity(hass, coordinator, entry, appliance, description, invert)
            )

    async_add_entities(appliances)


class HonSwitchEntity(HonBaseSwitchEntity):
    entity_description: HonSwitchEntityDescription

    def __init__(self, hass, coordinator, entry, appliance, entity_description, invert = False) -> None:
        super().__init__(coordinator, appliance, entity_description)
        self.invert = invert

    def _setting_key(self) -> str:
        return f"settings.{self.entity_description.key}"

    def _setting(self):
        return self._device.settings.get(self._setting_key())

    def _target_value(self, turn_on: bool) -> str:
        value = "1" if turn_on else "0"
        if self.invert:
            value = "0" if turn_on else "1"
        return value

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        if( self.invert == True ):
            return self._device.get(self.entity_description.key, "1") == "0"
        return self._device.get(self.entity_description.key, "0") == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        setting = self._setting()
        if setting is not None:
            if type(setting) == HonParameter:
                return
            if self.invert:
                setting.value = setting.min if isinstance(setting, HonParameterRange) else 0
            else:
                setting.value = setting.max if isinstance(setting, HonParameterRange) else 1
            await self._device.commands["settings"].send()
            value = str(setting.value)
        else:
            value = self._target_value(True)
            await self.coordinator.async_set({self.entity_description.key: value})

        self._device.set(self.entity_description.key, value)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        setting = self._setting()
        if setting is not None:
            if type(setting) == HonParameter:
                return
            if self.invert:
                setting.value = setting.max if isinstance(setting, HonParameterRange) else 1
            else:
                setting.value = setting.min if isinstance(setting, HonParameterRange) else 0
            await self._device.commands["settings"].send()
            value = str(setting.value)
        else:
            value = self._target_value(False)
            await self.coordinator.async_set({self.entity_description.key: value})

        self._device.set(self.entity_description.key, value)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        if not self._device.get("remoteCtrValid", "1") == "1":
            return False
        if self._device.get("attributes.lastConnEvent.category") == "DISCONNECTED":
            return False
        
        setting = self._setting()

        if setting is None:
            return self._device.get(self.entity_description.key, None) is not None

        #_LOGGER.warning(setting)
        #if isinstance(setting, HonParameterRange) and len(setting.values) < 2:
        #    return False
        return True

    @callback
    def _handle_coordinator_update(self, update: bool = True) -> None:
        self._attr_is_on = self.is_on
        if update:
            self.async_write_ha_state()
