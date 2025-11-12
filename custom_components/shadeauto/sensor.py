from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .coordinator import ShadeAutoCoordinator


def _raw_to_percent(raw):
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if 0 <= val <= 100:
        return int(round(val))
    if 2.5 <= val <= 5.5:
        lo, hi = 3.30, 4.20
        pct = (max(min(val, hi), lo) - lo) / (hi - lo) * 100
        return int(round(pct))
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    data = hass.data[DOMAIN][entry.entry_id]
    coord: ShadeAutoCoordinator = data["coordinator"]

    ents: list[ShadeAutoBattery] = []
    for uid, meta in coord.peripherals.items():
        ents.append(ShadeAutoBattery(coord, entry, uid, f"{meta.get('name','Shade')} Battery"))
    add_entities(ents)


class ShadeAutoBattery(CoordinatorEntity[ShadeAutoCoordinator], SensorEntity):
    _attr_should_poll = False
    _attr_device_class = "battery"
    _attr_state_class = "measurement"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: ShadeAutoCoordinator, entry: ConfigEntry, uid: str, name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._uid = uid
        self._attr_name = name
        thing = coordinator.data.get("thing_name") or coordinator.api.host
        self._attr_unique_id = f"shadeauto_{thing}_{uid}_battery"

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
    def native_value(self):
        st = self.coordinator.data.get("status", {}).get(self._uid, {})
        raw = st.get("BatteryVoltage")
        return _raw_to_percent(raw)
