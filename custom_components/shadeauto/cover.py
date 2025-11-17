from __future__ import annotations

from typing import Any
import time
import logging
import asyncio

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
import logging

_LOGGER = logging.getLogger(__name__)

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
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: ShadeAutoCoordinator, entry: ConfigEntry, uid: str, name: str) -> None:
        super().__init__(coordinator)
        self._uid = str(uid)
        self._entry = entry
        self._attr_name = name

        thing = coordinator.data.get("thing_name") or coordinator.api.host
        self._attr_unique_id = f"shadeauto_{thing}_{uid}"

        # UI estimation state
        self._est_start_pos: float | None = None
        self._est_target: float | None = None
        self._est_start_ts: float | None = None
        self._est_travel_time_s: float | None = None

        # animation task for smooth slider
        self._anim_task: asyncio.Task | None = None

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

    # --- estimation helpers ---

    def _estimation_config(self) -> tuple[bool, float]:
        """Read estimation settings from options (or defaults)."""
        from .const import DEFAULT_FULL_TRAVEL_TIME, DEFAULT_ESTIMATION_ENABLED

        est_enabled = DEFAULT_ESTIMATION_ENABLED
        full_time = DEFAULT_FULL_TRAVEL_TIME
        try:
            opts = self._entry.options
            est_enabled = bool(opts.get("estimation_enabled", DEFAULT_ESTIMATION_ENABLED))
            full_time = float(opts.get("full_travel_time_sec", DEFAULT_FULL_TRAVEL_TIME))
        except Exception:
            pass
        return est_enabled, full_time

    def _current_estimated_position(self) -> int | None:
        """Return an estimated position based on last command and elapsed time."""
        est_enabled, _full_time = self._estimation_config()
        now = time.monotonic()

        if not est_enabled or self._est_start_pos is None or self._est_target is None or self._est_start_ts is None:
            # Fall back to coordinator's effective position
            _LOGGER.debug("current_estimated_position: uid=%s Fallback", self._uid)
            try:
                return self.coordinator.get_effective_position(self._uid)
            except Exception:
                st = self._status_for_uid()
                raw = st.get("BottomRailPosition")
                try:
                    return int(raw) if raw is not None else None
                except (TypeError, ValueError):
                    return None

        if self._est_travel_time_s is None or self._est_travel_time_s <= 0:
            _LOGGER.debug("current_estimated_position: uid=%s No Travel Time", self._uid)
            return int(round(self._est_target))

        dt = max(0.0, now - self._est_start_ts)
        if dt >= self._est_travel_time_s:
            _LOGGER.debug("current_estimated_position: uid=%s Complete Travel Time", self._uid)
            return int(round(self._est_target))

        ratio = max(0.0, min(dt / self._est_travel_time_s, 1.0))
        est = self._est_start_pos + (self._est_target - self._est_start_pos) * ratio
        try:
            _LOGGER.debug("current_estimated_position: uid=%s Estimate = %s", self._uid, est)
            return int(round(est))
        except (TypeError, ValueError):
            return int(round(self._est_target))

    def _start_estimation_for_command(self, target: int) -> None:
        """Start or update estimation state when a new command is issued."""
        est_enabled, full_time = self._estimation_config()
        if not est_enabled:
            self._est_start_pos = None
            self._est_target = None
            self._est_start_ts = None
            self._est_travel_time_s = None
            return

        now = time.monotonic()

        # Estimate starting position for this command: if we have an ongoing estimation, advance it;
        # otherwise fall back to the last known position.
        start_pos: float | None = None
        if self._est_start_pos is not None and self._est_target is not None and self._est_start_ts is not None and self._est_travel_time_s:
            dt_prev = max(0.0, now - self._est_start_ts)
            if self._est_travel_time_s > 0:
                ratio_prev = max(0.0, min(dt_prev / self._est_travel_time_s, 1.0))
                start_pos = self._est_start_pos + (self._est_target - self._est_start_pos) * ratio_prev

        if start_pos is None:
            # Use the last effective position from the coordinator (or raw hub) as a baseline
            try:
                base = self.coordinator.get_effective_position(self._uid)
            except Exception:
                base = None
            if base is None:
                st = self._status_for_uid()
                raw = st.get("BottomRailPosition")
                try:
                    base = int(raw) if raw is not None else 0
                except (TypeError, ValueError):
                    base = 0
            start_pos = float(base)

        target_i = int(target)

        self._est_start_pos = start_pos
        self._est_target = float(target_i)
        self._est_start_ts = now

        distance = abs(target_i - start_pos)
        if distance <= 0:
            self._est_travel_time_s = 0.0
        else:
            self._est_travel_time_s = full_time * distance / 100.0

        _LOGGER.debug(
            "estimation: uid=%s start_pos=%.2f target=%s distance=%.2f full_time=%.2f travel_time_s=%.2f",
            self._uid,
            self._est_start_pos,
            target_i,
            distance,
            full_time,
            self._est_travel_time_s,
        )

    def _start_animation(self) -> None:
        """Start or restart the smooth slider animation for this move."""
        # Cancel any previous animation
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()

        est_enabled, _ = self._estimation_config()
        if (
            not est_enabled
            or self._est_start_ts is None
            or self._est_travel_time_s is None
            or self._est_travel_time_s <= 0
        ):
            self._anim_task = None
            return

        # Kick off an async loop to refresh the state a few times per second
        self._anim_task = asyncio.create_task(self._animate_slider())

    async def _animate_slider(self) -> None:
        """Periodically refresh HA state so the slider moves smoothly."""
        try:
            _LOGGER.debug("_animate_slider: uid=%s Starting", self._uid)
            # Aim for ~10–20 updates over the travel window, but bound the interval
            min_interval = 0.1
            max_interval = 0.5
            if self._est_travel_time_s is None or self._est_travel_time_s <= 0:
                interval = 0.3
            else:
                interval = max(
                    min_interval,
                    min(max_interval, self._est_travel_time_s / 20.0),
                )

            while True:
                # Push current estimated position & state to HA
                self.async_write_ha_state()

                # Stop if we no longer have an active estimation
                if (
                    self._est_start_ts is None
                    or self._est_travel_time_s is None
                    or self._est_travel_time_s <= 0
                ):
                    break

                dt = time.monotonic() - self._est_start_ts
                if dt >= self._est_travel_time_s:
                    # One last update at/near the target then exit
                    break

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            # New command started, animation replaced
            pass
        finally:
            # Final state write to ensure we end exactly at the target estimate
            self.async_write_ha_state()
            self._anim_task = None
            _LOGGER.debug("_animate_slider: uid=%s Finished", self._uid)
            
    # --- core properties ---

    @property
    def current_cover_position(self) -> int | None:
        """Report the shade position, using time-based estimation when enabled."""
        pos = self._current_estimated_position()
        _LOGGER.debug("pos_debug: current_cover_position uid=%s -> %s", self._uid, pos)
        return pos

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos == 0

    @property
    def is_opening(self) -> bool | None:
        est_enabled, _ = self._estimation_config()
        if not est_enabled or self._est_start_pos is None or self._est_target is None or self._est_start_ts is None or self._est_travel_time_s is None:
            return None
        now = time.monotonic()
        if now - self._est_start_ts >= self._est_travel_time_s:
            return None
        # target greater than start => opening
        val = self._est_target > self._est_start_pos
        return val

    @property
    def is_closing(self) -> bool | None:
        est_enabled, _ = self._estimation_config()
        if not est_enabled or self._est_start_pos is None or self._est_target is None or self._est_start_ts is None or self._est_travel_time_s is None:
            return None
        now = time.monotonic()
        if now - self._est_start_ts >= self._est_travel_time_s:
            return None
        # target less than start => closing
        val = self._est_target < self._est_start_pos
        return val

    # --- commands ---

    async def async_set_cover_position(self, **kwargs) -> None:
        pos = int(kwargs["position"])
        self._start_estimation_for_command(pos)
        self._start_animation()
        self.async_write_ha_state()
        await self.coordinator.api.control(self._uid, bottom=pos)
        self.coordinator.register_command(self._uid, pos)

    async def async_open_cover(self, **kwargs) -> None:
        target = 100
        self._start_estimation_for_command(target)
        self._start_animation()
        self.async_write_ha_state()
        await self.coordinator.api.control(self._uid, bottom=target)
        self.coordinator.register_command(self._uid, target)

    async def async_close_cover(self, **kwargs) -> None:
        target = 0
        self._start_estimation_for_command(target)
        self._start_animation()
        self.async_write_ha_state()
        await self.coordinator.api.control(self._uid, bottom=target)
        self.coordinator.register_command(self._uid, target)

