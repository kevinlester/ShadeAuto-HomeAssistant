from __future__ import annotations

from typing import Any

import time

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

# UI-only speed assumption: 0 -> 100 in ~25s  =>  0.25s per percent
_UI_SECONDS_PER_PERCENT = 0.25


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coord: ShadeAutoCoordinator = data["coordinator"]
    entities: list[ShadeAutoCover] = []
    for uid, meta in coord.peripherals.items():
        name = meta.get("name", f"Shade {uid}")
        entities.append(ShadeAutoCover(coord, entry, uid, name))
    add_entities(entities)


class ShadeAutoCover(CoordinatorEntity[ShadeAutoCoordinator], CoverEntity):
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        coordinator: ShadeAutoCoordinator,
        entry: ConfigEntry,
        uid: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._uid = str(uid)
        self._entry = entry
        self._attr_name = name

        # This line may already match your working version; keep it.
        thing = (
            coordinator.data.get("thing_name")
            or getattr(coordinator.api, "thing_name", None)
            or "ShadeAutoHub"
        )
        self._attr_unique_id = f"shadeauto_{thing}_{uid}"

        # UI-only motion state (for opening/closing)
        self._ui_motion_dir: int = 0           # +1 opening, -1 closing, 0 idle
        self._ui_motion_until: float | None = None  # monotonic timestamp

    @property
    def device_info(self) -> DeviceInfo:
        host = self.coordinator.api.host
        return DeviceInfo(
            identifiers={(DOMAIN, f"shade_{host}_{self._uid}")},
            via_device=(DOMAIN, f"hub_{host}"),
            name=self.name,
            manufacturer="Norman (ShadeAuto)",
            model=str(
                self.coordinator.peripherals.get(self._uid, {}).get("module_detail") or "Shade"
            ),
        )

    def _status_for_uid(self) -> dict[str, Any]:
        return self.coordinator.data.get("status", {}).get(self._uid, {})

    @property
    def available(self) -> bool:
        return self._uid in self.coordinator.data.get("status", {})

    @property
    def current_cover_position(self) -> int | None:
        """
        Report the shade position.

        If your working coordinator already exposes a get_effective_position()
        that fakes the position while in motion, this will use it.
        Otherwise it falls back to raw BottomRailPosition.
        """
        # Prefer the coordinator helper if present (Option B build)
        get_eff = getattr(self.coordinator, "get_effective_position", None)
        if callable(get_eff):
            try:
                return get_eff(self._uid)
            except Exception:
                pass

        # Fallback to raw hub status
        pos = self._status_for_uid().get("BottomRailPosition")
        try:
            return int(pos) if pos is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos == 0

    # ---- UI motion helper -------------------------------------------------

    def _start_ui_motion(self, target: int) -> None:
        """
        Start (or override) the opening/closing indication based on distance.

        - Uses the *current* position as the starting point.
        - Computes a duration of _UI_SECONDS_PER_PERCENT * |Δ|.
        - Sets a simple one-shot timer for is_opening/is_closing to consult.
        """
        try:
            start = self.current_cover_position
        except Exception:
            start = None

        if start is None:
            start = 0

        try:
            start_i = int(start)
        except (TypeError, ValueError):
            start_i = 0

        target_i = int(target)
        distance = abs(target_i - start_i)

        if distance == 0:
            # No movement → no opening/closing state
            self._ui_motion_dir = 0
            self._ui_motion_until = None
            return

        self._ui_motion_dir = 1 if target_i > start_i else -1
        duration = distance * _UI_SECONDS_PER_PERCENT
        if duration <= 0:
            duration = _UI_SECONDS_PER_PERCENT

        self._ui_motion_until = time.monotonic() + duration

    # ---- Opening / closing state (time-based UI only) ---------------------

    @property
    def is_opening(self) -> bool | None:
        state_until = self._ui_motion_until
        if self._ui_motion_dir <= 0 or state_until is None:
            return None
        if time.monotonic() >= state_until:
            return None
        return True

    @property
    def is_closing(self) -> bool | None:
        state_until = self._ui_motion_until
        if self._ui_motion_dir >= 0 or state_until is None:
            return None
        if time.monotonic() >= state_until:
            return None
        return True

    # ---- Commands ---------------------------------------------------------

    async def async_set_cover_position(self, **kwargs) -> None:
        pos = int(kwargs["position"])
        # Start UI motion first so HA sees the state change immediately
        self._start_ui_motion(pos)
        await self.coordinator.api.control(self._uid, bottom=pos)
        self.coordinator.register_command(self._uid, pos)

    async def async_open_cover(self, **kwargs) -> None:
        target = 100
        self._start_ui_motion(target)
        await self.coordinator.api.control(self._uid, bottom=target)
        self.coordinator.register_command(self._uid, target)

    async def async_close_cover(self, **kwargs) -> None:
        target = 0
        self._start_ui_motion(target)
        await self.coordinator.api.control(self._uid, bottom=target)
        self.coordinator.register_command(self._uid, target)
