from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShadeAutoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    data = hass.data[DOMAIN][entry.entry_id]
    coord: ShadeAutoCoordinator = data["coordinator"]

    ents: list[ShadeAutoBattery] = []
    for uid, meta in coord.peripherals.items():
        ents.append(ShadeAutoBattery(coord, uid, f"{meta.get('name','Shade')} Battery"))
    add_entities(ents)


class ShadeAutoBattery(CoordinatorEntity[ShadeAutoCoordinator], SensorEntity):
    _attr_should_poll = False
    _attr_device_class = "battery"
    _attr_state_class = "measurement"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str, name: str) -> None:
        super().__init__(coordinator)
        self._uid = uid
        self._attr_name = name
        self._attr_unique_id = f"shadeauto_{coordinator.api.host}_{uid}_battery"

    @property
    def available(self) -> bool:
        return self._uid in self.coordinator.data.get("status", {})

    @property
    def native_value(self):
        st = self.coordinator.data.get("status", {}).get(self._uid, {})
        raw = st.get("BatteryVoltage")
        if raw is None:
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        # If hub already reports percent (0..100), clamp and return
        if 0 <= val <= 100:
            return int(round(val))
        # Fallback: convert volts (typical Li-ion 3.3–4.2 V) → %
        if 2.5 <= val <= 5.5:
            lo, hi = 3.30, 4.20
            pct = (max(min(val, hi), lo) - lo) / (hi - lo) * 100
            return int(round(pct))
        return None
