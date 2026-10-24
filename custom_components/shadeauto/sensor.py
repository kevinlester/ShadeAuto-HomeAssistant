"""Battery level sensor, one per shade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_BATTERY
from .coordinator import ShadeAutoCoordinator
from .entity import ShadeAutoEntity
from .util import battery_percent

if TYPE_CHECKING:
    from . import ShadeAutoConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ShadeAutoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(ShadeAutoBattery(coordinator, uid) for uid in coordinator.peripherals)


class ShadeAutoBattery(ShadeAutoEntity, SensorEntity):
    """The shade motor's battery level in percent."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "battery"

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid, unique_id_suffix="_battery")

    @property
    def native_value(self) -> int | None:
        return battery_percent(self._status.get(ATTR_BATTERY))
