from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShadeAutoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    data = hass.data[DOMAIN][entry.entry_id]
    coord: ShadeAutoCoordinator = data["coordinator"]
    entities: list[ShadeAutoCover] = []
    for uid, meta in coord.peripherals.items():
        name = meta.get("name", f"Shade {uid}")
        entities.append(ShadeAutoCover(coord, entry, uid, name))
    add_entities(entities)


class ShadeAutoCover(CoordinatorEntity[ShadeAutoCoordinator], CoverEntity):
    _attr_should_poll = False
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    def __init__(self, coordinator: ShadeAutoCoordinator, entry: ConfigEntry, uid: str, name: str) -> None:
        super().__init__(coordinator)
        self._uid = str(uid)
        self._entry = entry
        self._attr_name = name

        thing = coordinator.data.get("thing_name") or coordinator.api.thing_name or "ShadeAutoHub"
        self._attr_unique_id = f"shadeauto_{thing}_{uid}"

    @property
    def device_info(self) -> DeviceInfo:
        host = self.coordinator.api.host
        return DeviceInfo(
            identifiers={(DOMAIN, f"shade_{host}_{self._uid}")},
            via_device=(DOMAIN, f"hub_{host}"),
            name=self.name,
            manufacturer="Norman (ShadeAuto)",
            model=str(self.coordinator.peripherals.get(self._uid, {}).get("module_detail") or "Shade"),
        )

    def _status_for_uid(self) -> dict[str, Any]:
        return self.coordinator.data.get("status", {}).get(self._uid, {})

    @property
    def available(self) -> bool:
        return self._uid in self.coordinator.data.get("status", {})

    @property
    def current_cover_position(self) -> int | None:
        """Report the shade position, faking while in motion if needed."""
        try:
            return self.coordinator.get_effective_position(self._uid)
        except Exception:
            # Fallback to raw hub status if motion state isn't available
            pos = self._status_for_uid().get("BottomRailPosition")
            try:
                return int(pos) if pos is not None else None
            except (TypeError, ValueError):
                return None

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos == 0

    @property
    def is_opening(self) -> bool | None:
        """Return True if the cover is currently opening."""
        state = self.coordinator.get_motion_state(self._uid)
        if not state or not state.in_motion:
            return None
        if state.pending_target is None or state.last_hub_pos is None:
            return None
        # Higher BottomRailPosition = more open
        return state.pending_target > state.last_hub_pos

    @property
    def is_closing(self) -> bool | None:
        """Return True if the cover is currently closing."""
        state = self.coordinator.get_motion_state(self._uid)
        if not state or not state.in_motion:
            return None
        if state.pending_target is None or state.last_hub_pos is None:
            return None
        # Lower BottomRailPosition = more closed
        return state.pending_target < state.last_hub_pos

    async def async_set_cover_position(self, **kwargs):
        pos = int(kwargs["position"])
        await self.coordinator.api.control(self._uid, bottom=pos)
        self.coordinator.register_command(self._uid, pos)

    async def async_open_cover(self, **kwargs):
        await self.coordinator.api.control(self._uid, bottom=100)
        self.coordinator.register_command(self._uid, 100)

    async def async_close_cover(self, **kwargs):
        await self.coordinator.api.control(self._uid, bottom=0)
        self.coordinator.register_command(self._uid, 0)
