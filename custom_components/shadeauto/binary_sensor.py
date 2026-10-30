"""Low-battery binary sensor, one per shade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
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
    async_add_entities(ShadeAutoBatteryLow(coordinator, uid) for uid in coordinator.peripherals)


class ShadeAutoBatteryLow(ShadeAutoEntity, BinarySensorEntity):
    """On when the battery is at or below the configured threshold."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "battery_low"

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid, unique_id_suffix="_battery_low")

    @property
    def is_on(self) -> bool | None:
        percent = battery_percent(self._status.get(ATTR_BATTERY))
        if percent is None:
            return None
        return percent <= self.coordinator.options.low_battery_threshold
