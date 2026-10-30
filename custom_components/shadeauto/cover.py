"""Cover entities: one per shade, driving the bottom rail.

The hub reports a shade's position only after it has moved, so while a
command is in flight the entity interpolates the slider from the last known
position to the target over ``full_travel_time_sec`` ("estimation"). The
coordinator's view wins whenever estimation is off or has no active move.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import ATTR_POSITION, CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ShadeAutoError
from .const import DOMAIN
from .coordinator import ShadeAutoCoordinator
from .entity import ShadeAutoEntity

if TYPE_CHECKING:
    from . import ShadeAutoConfigEntry

_LOGGER = logging.getLogger(__name__)

# Slider animation refresh bounds: fast enough to look smooth, slow enough not to flood the state machine.
_ANIMATION_MIN_INTERVAL = 0.1
_ANIMATION_MAX_INTERVAL = 0.5
_ANIMATION_FRAMES = 20


async def async_setup_entry(
    hass: HomeAssistant, entry: ShadeAutoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(ShadeAutoCover(coordinator, uid) for uid in coordinator.peripherals)


class ShadeAutoCover(ShadeAutoEntity, CoverEntity):
    """A shade's bottom rail as a Home Assistant cover."""

    _attr_name = None  # the main entity carries the device name
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid)
        # Estimation of the current move: start position, target, start time, duration.
        self._est_start_pos: float | None = None
        self._est_target: float | None = None
        self._est_start_ts: float | None = None
        self._est_travel_time_s: float | None = None
        self._anim_task: asyncio.Task[None] | None = None
        self._removed = False

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._cancel_animation()
        await super().async_will_remove_from_hass()

    # --- estimation ---------------------------------------------------------

    def _estimation_active(self) -> bool:
        """True while estimation is enabled and a move is still being interpolated."""
        if not self.coordinator.options.estimation_enabled:
            return False
        if self._est_start_pos is None or self._est_target is None or self._est_start_ts is None:
            return False
        if self._est_travel_time_s is None or self._est_travel_time_s <= 0:
            return False
        return time.monotonic() - self._est_start_ts < self._est_travel_time_s

    def _current_estimated_position(self) -> int | None:
        """Interpolated position during a move; the coordinator's value otherwise."""
        if (
            not self.coordinator.options.estimation_enabled
            or self._est_start_pos is None
            or self._est_target is None
            or self._est_start_ts is None
        ):
            return self.coordinator.get_effective_position(self._uid)
        if not self._est_travel_time_s or self._est_travel_time_s <= 0:
            return round(self._est_target)
        elapsed = max(0.0, time.monotonic() - self._est_start_ts)
        if elapsed >= self._est_travel_time_s:
            return round(self._est_target)
        ratio = min(elapsed / self._est_travel_time_s, 1.0)
        return round(self._est_start_pos + (self._est_target - self._est_start_pos) * ratio)

    def _start_estimation_for_command(self, target: int) -> None:
        """Begin interpolating toward ``target`` from wherever the shade is believed to be now."""
        options = self.coordinator.options
        if not options.estimation_enabled:
            self._clear_estimation()
            return

        now = time.monotonic()
        start_pos: float | None = None
        # A command issued mid-move starts from the interpolated position, not the old baseline.
        if (
            self._est_start_pos is not None
            and self._est_target is not None
            and self._est_start_ts is not None
            and self._est_travel_time_s
        ):
            ratio_prev = min(max(0.0, now - self._est_start_ts) / self._est_travel_time_s, 1.0)
            start_pos = self._est_start_pos + (self._est_target - self._est_start_pos) * ratio_prev
        if start_pos is None:
            start_pos = float(self.coordinator.get_effective_position(self._uid) or 0)

        self._est_start_pos = start_pos
        self._est_target = float(target)
        self._est_start_ts = now
        distance = abs(target - start_pos)
        self._est_travel_time_s = options.full_travel_time_sec * distance / 100.0 if distance > 0 else 0.0
        _LOGGER.debug("UID %s estimation: %.1f -> %s over %.1fs", self._uid, start_pos, target, self._est_travel_time_s)

    def _clear_estimation(self) -> None:
        self._est_start_pos = None
        self._est_target = None
        self._est_start_ts = None
        self._est_travel_time_s = None

    def _start_animation(self) -> None:
        """Restart the task that re-renders the slider during the estimated move."""
        self._cancel_animation()
        if not self._estimation_active():
            return
        self._anim_task = self.coordinator.config_entry.async_create_background_task(
            self.hass, self._animate_slider(), name=f"{DOMAIN} slider {self._uid}"
        )

    def _cancel_animation(self) -> None:
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()
        self._anim_task = None

    async def _animate_slider(self) -> None:
        """Write state a few times per second until the estimated move completes."""
        assert self._est_travel_time_s  # guaranteed by _start_animation
        interval = max(
            _ANIMATION_MIN_INTERVAL, min(_ANIMATION_MAX_INTERVAL, self._est_travel_time_s / _ANIMATION_FRAMES)
        )
        try:
            while self._estimation_active():
                self.async_write_ha_state()
                await asyncio.sleep(interval)
        finally:
            if not self._removed:
                # One last write so the slider ends exactly on the target.
                self.async_write_ha_state()

    # --- state ------------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        return self._current_estimated_position()

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos == 0

    @property
    def is_opening(self) -> bool | None:
        if not self._estimation_active():
            return None
        assert self._est_target is not None and self._est_start_pos is not None
        return self._est_target > self._est_start_pos

    @property
    def is_closing(self) -> bool | None:
        if not self._estimation_active():
            return None
        assert self._est_target is not None and self._est_start_pos is not None
        return self._est_target < self._est_start_pos

    # --- commands ---------------------------------------------------------------

    async def _async_move_to(self, target: int) -> None:
        """Send the bottom rail to ``target`` and start the optimistic UI update."""
        target = max(0, min(100, int(target)))
        self._start_estimation_for_command(target)
        self._start_animation()
        self.async_write_ha_state()
        try:
            await self.coordinator.api.control(self._uid, bottom=target)
        except ShadeAutoError as err:
            raise HomeAssistantError(f"Could not send command to shade {self._uid}: {err}") from err
        self.coordinator.register_command(self._uid, target)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_move_to(kwargs[ATTR_POSITION])

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_move_to(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_move_to(0)
