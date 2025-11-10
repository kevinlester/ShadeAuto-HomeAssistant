from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .coordinator import ShadeAutoCoordinator
from .sensor import _raw_to_percent


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    data = hass.data[DOMAIN][entry.entry_id]
    coord: ShadeAutoCoordinator = data["coordinator"]

    ents: list[ShadeAutoBatteryLow] = []
    for uid, meta in coord.peripherals.items():
        ents.append(ShadeAutoBatteryLow(coord, entry, uid, f"{meta.get('name','Shade')} Battery Low"))
    add_entities(ents)


class ShadeAutoBatteryLow(CoordinatorEntity[ShadeAutoCoordinator], BinarySensorEntity):
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator: ShadeAutoCoordinator, entry: ConfigEntry, uid: str, name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._uid = uid
        self._attr_name = name
        self._attr_unique_id = f"shadeauto_{coordinator.api.host}_{uid}_battery_low"

    @property
    def available(self) -> bool:
        return self._uid in self.coordinator.data.get("status", {})

    @property
    def device_info(self) -> DeviceInfo:
        host = self.coordinator.api.host
        return DeviceInfo(
            identifiers={(DOMAIN, f"shade_{host}_{self._uid}")},
            via_device=(DOMAIN, f"hub_{host}"),
            name=self.coordinator.peripherals.get(self._uid, {}).get("name", "Shade"),
            manufacturer="Norman (ShadeAuto)",
            model=str(self.coordinator.peripherals.get(self._uid, {}).get("module_detail") or "Shade"),
        )

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data.get("status", {}).get(self._uid, {})
        raw = st.get("BatteryVoltage")
        pct = _raw_to_percent(raw)
        if pct is None:
            return None
        threshold = int(self._entry.options.get("low_battery_threshold", 20))
        return pct <= threshold
