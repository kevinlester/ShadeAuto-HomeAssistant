"""Cover entities, chosen per shade by its profile (``Peripheral.profile``).

* **sheer** (PerfectSheer): one cover, lift on the bottom rail and vane tilt
  on the middle rail;
* **single_rail** and **legacy** (unrecognised shades): one lift-only cover
  that sends bottom-only commands, as 0.3.0 did;
* **two_rail** (top-down/bottom-up, day/night): two covers on one device, the
  main one for the bottom rail and a "Top" one for the middle rail. Both
  rails are heights on the same axis, 0 lowered to 100 raised.

Positions come from the hub. Because the hub reports only end states, a
cover interpolates its position slider during a move ("estimation") from the
last known position to the target over ``full_travel_time_sec``. The
estimate is dropped as soon as the coordinator no longer sees the cover's
axis moving, or as soon as the hub's target is no longer the one being
estimated toward (the app or a remote re-commanded the shade mid-move), so
the hub always wins once it has something to say.

Hub rules that shape the commands (see ``docs/HUB_PROTOCOL.md``):

* every command must carry ``BottomRailPosition``, and leaving out
  ``MiddleRailPosition`` makes the hub set the middle target to 0: that
  closes a sheer shade's vanes and drags a two-rail shade's top rail down, so
  both of those always send both rails;
* vanes are honoured only while the shade is fully lowered, so a tilt command
  is sent as ``(bottom 0, middle tilt)`` and lowers the shade if needed, as
  ``homeassistant.components.hunterdouglas_powerview.cover.PowerViewShadeWithTiltOnClosed``
  does for shades that tilt only when closed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ShadeAutoError, clamp_position
from .const import (
    AXIS_BOTTOM,
    AXIS_MIDDLE,
    DOMAIN,
    PROFILE_SHEER,
    PROFILE_TWO_RAIL,
    TILT_MODE_AUTO,
    TOP_COVER_UNIQUE_ID_SUFFIX,
)
from .coordinator import ShadeAutoCoordinator
from .entity import ShadeAutoEntity

if TYPE_CHECKING:
    from . import ShadeAutoConfigEntry

_LOGGER = logging.getLogger(__name__)

LIFT_FEATURES = (
    CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP
)
TILT_FEATURES = (
    CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.SET_TILT_POSITION
    # STOP_TILT sends the same MotorStop as STOP (one motor, one command). On the
    # hub it was verified stopping a lift; a stop during a vane move was not
    # tried separately (docs/HUB_PROTOCOL.md, open question 12).
    | CoverEntityFeature.STOP_TILT
)

# How a two-rail shade's rails are named in messages to the user.
_RAIL_NAMES = {AXIS_BOTTOM: "bottom", AXIS_MIDDLE: "top"}

# Slider animation refresh bounds: fast enough to look smooth, slow enough not to flood the state machine.
_ANIMATION_MIN_INTERVAL = 0.1
_ANIMATION_MAX_INTERVAL = 0.5
_ANIMATION_FRAMES = 20


@dataclass(frozen=True, slots=True)
class _Estimate:
    """One interpolated move: from ``start_pos`` at ``start_ts`` to ``target`` over ``travel_time_s``."""

    start_pos: float
    target: int
    start_ts: float
    travel_time_s: float

    def finished(self, now: float) -> bool:
        return now - self.start_ts >= self.travel_time_s

    def position_at(self, now: float) -> float:
        """Where the rail is believed to be at ``now``."""
        if self.finished(now):
            # Travel time elapsed but the hub has not reported the end state
            # yet (it only does so once the motor stops): hold the slider at
            # the target rather than snapping back to the stale actual.
            return float(self.target)
        ratio = max(0.0, now - self.start_ts) / self.travel_time_s
        return self.start_pos + (self.target - self.start_pos) * ratio


def plan_rail_move(axis: str, target: int, held: int, *, guard: bool, gap: int) -> tuple[int, int]:
    """The ``(bottom, middle)`` pair that moves one rail of a two-rail shade to ``target``.

    The other rail is restated at ``held``, where it is heading anyway. With
    the collision guard on, the middle rail must stay at least ``gap`` above
    the bottom rail, and the other rail gives way rather than the request
    being refused (upstream PR #9): raising the bottom rail drags the middle
    rail up to ``bottom + gap``, lowering the middle rail pushes the bottom
    rail down to ``middle - gap``. The moved rail itself stops ``gap`` short
    of the far end, since the other rail cannot go past 0 or 100. With the
    guard off the pair is exactly the request plus ``held``.
    """
    target, held = clamp_position(target), clamp_position(held)
    if axis == AXIS_BOTTOM:
        if not guard:
            return target, held
        bottom = min(target, 100 - gap)
        return bottom, min(100, max(held, bottom + gap))
    if not guard:
        return held, target
    middle = max(target, gap)
    return max(0, min(held, middle - gap)), middle


async def async_setup_entry(
    hass: HomeAssistant, entry: ShadeAutoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(cover for uid in coordinator.peripherals for cover in _covers_for(coordinator, uid))


def _covers_for(coordinator: ShadeAutoCoordinator, uid: str) -> list[ShadeAutoCover]:
    profile = coordinator.peripherals[uid].profile
    if profile == PROFILE_SHEER:
        return [SheerCover(coordinator, uid)]
    if profile == PROFILE_TWO_RAIL:
        return [TwoRailBottomCover(coordinator, uid), TwoRailTopCover(coordinator, uid)]
    return [LiftCover(coordinator, uid)]


class ShadeAutoCover(ShadeAutoEntity, CoverEntity):
    """What every shade cover shares: one axis it shows and estimates, motion flags, sending and stop.

    Subclasses map the cover services onto ``(bottom, middle)`` commands.
    """

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = LIFT_FEATURES
    _axis = AXIS_BOTTOM  # the axis whose position, motion and estimate this cover shows

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str, *, unique_id_suffix: str = "") -> None:
        super().__init__(coordinator, uid, unique_id_suffix=unique_id_suffix)
        self._estimate: _Estimate | None = None
        self._anim_task: asyncio.Task[None] | None = None
        self._removed = False

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._cancel_animation()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        # The one place a stale estimate is dropped (the hub reported the end of
        # the move, or its target is no longer ours), so the position property
        # can stay a pure read.
        self._estimate = self._valid_estimate()
        super()._handle_coordinator_update()

    # --- estimation (this cover's axis only) ------------------------------------

    def _valid_estimate(self) -> _Estimate | None:
        """The estimate while it still describes what the hub is doing, else ``None``."""
        estimate = self._estimate
        if estimate is None or not self.coordinator.options.estimation_enabled:
            return None
        if not self.coordinator.is_moving(self._uid, self._axis):
            return None
        if self.coordinator.get_target(self._uid, self._axis) != estimate.target:
            return None
        return estimate

    def _estimation_active(self) -> bool:
        """True while the slider still has frames to render; the animation's loop condition."""
        estimate = self._valid_estimate()
        return estimate is not None and not estimate.finished(time.monotonic())

    def _estimated_position(self) -> int | None:
        """Interpolated position while the estimate holds; the hub's value otherwise. A pure read."""
        estimate = self._valid_estimate()
        if estimate is None:
            return self.coordinator.get_position(self._uid, self._axis)
        return round(estimate.position_at(time.monotonic()))

    def _start_estimation(self, target: int) -> None:
        """Begin interpolating toward ``target`` from where the slider is now."""
        options = self.coordinator.options
        if not options.estimation_enabled:
            self._clear_estimation()
            return
        # A command issued mid-move starts from the interpolated position, not the stale hub actual.
        start_pos = float(self._estimated_position() or 0)
        distance = abs(target - start_pos)
        self._estimate = _Estimate(
            start_pos=start_pos,
            target=target,
            start_ts=time.monotonic(),
            travel_time_s=options.full_travel_time_sec * distance / 100.0,
        )
        _LOGGER.debug(
            "UID %s %s estimation: %.1f -> %s over %.1fs",
            self._uid,
            self._axis,
            start_pos,
            target,
            self._estimate.travel_time_s,
        )

    def _clear_estimation(self) -> None:
        self._estimate = None

    def _start_animation(self) -> None:
        """Restart the task that re-renders the slider during the estimated move."""
        self._cancel_animation()
        estimate = self._valid_estimate()
        if estimate is None or estimate.finished(time.monotonic()):
            return
        frame = estimate.travel_time_s / _ANIMATION_FRAMES
        interval = max(_ANIMATION_MIN_INTERVAL, min(_ANIMATION_MAX_INTERVAL, frame))
        self._anim_task = self.coordinator.config_entry.async_create_background_task(
            self.hass, self._animate_slider(interval), name=f"{DOMAIN} slider {self._uid} {self._axis}"
        )

    def _cancel_animation(self) -> None:
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()
        self._anim_task = None

    async def _animate_slider(self, interval: float) -> None:
        """Write state every ``interval`` seconds until the estimated move completes."""
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
        return self._estimated_position()

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos == 0

    def _direction(self) -> int:
        """+1 while this cover's axis heads higher, -1 while it heads lower, 0 when it is not moving."""
        if not self.coordinator.is_moving(self._uid, self._axis):
            return 0
        target = self.coordinator.get_target(self._uid, self._axis)
        actual = self.coordinator.get_position(self._uid, self._axis)
        if target is None or actual is None or target == actual:
            return 0
        return 1 if target > actual else -1

    @property
    def is_opening(self) -> bool | None:
        return self._direction() > 0

    @property
    def is_closing(self) -> bool | None:
        return self._direction() < 0

    # --- commands ---------------------------------------------------------------

    async def _async_send(self, *, bottom: int, middle: int | None) -> None:
        """Send one move and start the optimistic UI update; ``middle=None`` sends the bottom rail alone."""
        sent = {AXIS_BOTTOM: bottom, AXIS_MIDDLE: middle}
        own_target = sent[self._axis]
        # The estimate starts from where the slider is now, which the previous
        # command's target still defines, so it is taken before the new command
        # replaces that target.
        if own_target is None:
            self._clear_estimation()
        else:
            self._start_estimation(own_target)
        # Registering must come before _start_animation: the animation only runs
        # while the coordinator reports this cover's axis moving, which the
        # pending target guarantees until the hub reflects the command.
        cmd_id = self.coordinator.register_command(self._uid, bottom=bottom, middle=middle)
        self._start_animation()
        self.async_write_ha_state()
        try:
            await self.coordinator.api.control(self._uid, bottom=bottom, middle=middle)
        except ShadeAutoError as err:
            # The hub rejected the command or could not be reached: take the
            # optimistic state back, or the entity would show "opening" at the
            # commanded target until the next poll, a verify would retry a
            # command that never took, and the next command would restate its
            # vane or rail target.
            self._undo_command(cmd_id)
            raise HomeAssistantError(f"Could not move shade {self.shade_name}: {err}") from err
        except asyncio.CancelledError:
            # The caller gave up while the command waited for its send slot or
            # was in flight (a script was stopped, Home Assistant is shutting
            # down). Whether it reached the hub is unknown; if it did, the hub's
            # own target shows the move. A pending target left behind would
            # wait for a command_sent that never comes.
            self._undo_command(cmd_id)
            raise
        self.coordinator.command_sent(self._uid, cmd_id)
        # Read the hub right away: its target now reflects the command, and the
        # refresh re-arms the poll timer at the faster motion interval.
        await self.coordinator.async_refresh()

    def _undo_command(self, cmd_id: int) -> None:
        """Take back the optimistic state of a command whose post did not complete."""
        self.coordinator.discard_command(self._uid, cmd_id)
        self._clear_estimation()
        self._cancel_animation()
        self.async_write_ha_state()

    async def _async_stop(self) -> None:
        self._clear_estimation()
        self._cancel_animation()
        self.coordinator.register_stop(self._uid)
        self.async_write_ha_state()
        try:
            await self.coordinator.api.stop(self._uid)
        except ShadeAutoError as err:
            # register_stop has no undo (the stop may well have reached the hub),
            # so the motion flags stay off until the hub reports its next change,
            # as they do after a stop that did go through.
            raise HomeAssistantError(f"Could not stop shade {self.shade_name}: {err}") from err
        await self.coordinator.async_refresh()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        # One motor per shade and no per-rail stop: this stops the whole shade.
        await self._async_stop()


class SheerCover(ShadeAutoCover):
    """A sheer shade (PerfectSheer): lift on the bottom rail plus vane tilt, unless tilt mode is off.

    Every command carries both rails, so a lift never closes the vanes by
    leaving the middle rail out. With tilt mode off the commands are the
    same; only the tilt features and ``current_tilt_position`` go away.
    """

    _attr_name = None  # the main entity carries the device name

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid)
        self._has_tilt = coordinator.options.tilt_mode == TILT_MODE_AUTO
        self._attr_supported_features = LIFT_FEATURES | (TILT_FEATURES if self._has_tilt else CoverEntityFeature(0))

    @property
    def current_cover_tilt_position(self) -> int | None:
        if not self._has_tilt:
            return None
        return self.coordinator.get_position(self._uid, AXIS_MIDDLE)

    async def _async_command(self, *, bottom: int, middle: int) -> None:
        bottom, middle = clamp_position(bottom), clamp_position(middle)
        if bottom > 0 and middle > 0:
            # The hub would accept it and then reset the vanes anyway; be explicit instead.
            middle = 0
        await self._async_send(bottom=bottom, middle=middle)

    def _current_tilt_target(self) -> int:
        target = self.coordinator.get_target(self._uid, AXIS_MIDDLE)
        if target is None:
            target = self.coordinator.get_position(self._uid, AXIS_MIDDLE)
        return target or 0

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = clamp_position(kwargs[ATTR_POSITION])
        # Fully lowering keeps the vanes as they are; any other height closes them (hub rule).
        middle = self._current_tilt_target() if position == 0 else 0
        await self._async_command(bottom=position, middle=middle)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_command(bottom=100, middle=0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=self._current_tilt_target())

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=clamp_position(kwargs[ATTR_TILT_POSITION]))

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=100)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._async_command(bottom=0, middle=0)

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        await self._async_stop()


class LiftCover(ShadeAutoCover):
    """A single-rail or unrecognised shade: lift only, sent as bottom-only commands exactly like 0.3.0's.

    The middle rail is neither sent nor tracked: on these shades it always
    reads 0, and its target may be stuck at 100 by an app preset.
    """

    _attr_name = None  # the main entity carries the device name

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_send(bottom=clamp_position(kwargs[ATTR_POSITION]), middle=None)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_send(bottom=100, middle=None)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_send(bottom=0, middle=None)


class _TwoRailCover(ShadeAutoCover):
    """One rail of a two-rail shade, positioned 0 (lowered) to 100 (raised).

    Every command carries both rails: a bottom-only command would set the top
    rail's target to 0. The rail this cover does not drive is restated where
    it is heading (``coordinator.get_held_position``), moved only as far as
    the collision guard needs (:func:`plan_rail_move`). When that position is
    not known the command is refused rather than guessed.
    """

    async def _async_move_rail(self, target: int) -> None:
        other = AXIS_MIDDLE if self._axis == AXIS_BOTTOM else AXIS_BOTTOM
        held = self.coordinator.get_held_position(self._uid, other)
        if held is None:
            raise HomeAssistantError(
                f"Could not move shade {self.shade_name}: the hub has not reported where its "
                f"{_RAIL_NAMES[other]} rail is, and every command has to say where to hold it"
            )
        options = self.coordinator.options
        bottom, middle = plan_rail_move(
            self._axis, target, held, guard=options.collision_guard, gap=options.rail_min_gap
        )
        await self._async_send(bottom=bottom, middle=middle)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_move_rail(kwargs[ATTR_POSITION])

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_move_rail(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_move_rail(0)


class TwoRailBottomCover(_TwoRailCover):
    """The bottom rail of a two-rail shade; the shade's main cover, with the unique id single covers have."""

    _attr_name = None  # the main entity carries the device name


class TwoRailTopCover(_TwoRailCover):
    """The middle ("top") rail of a two-rail shade, named "<device> Top"."""

    _attr_translation_key = "top"
    _axis = AXIS_MIDDLE

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid, unique_id_suffix=TOP_COVER_UNIQUE_ID_SUFFIX)
