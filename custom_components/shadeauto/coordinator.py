"""Polling coordinator: fetches hub status and tracks commanded moves.

The hub is the source of truth. It reports two axes per shade, the bottom
rail and the middle rail (vane tilt on sheer shades, the second rail on
two-rail shades); for each it gives an actual position and a target. Which
axes are tracked depends on the shade's profile (:func:`shade_profile`):
single-rail and unrecognised shades track the bottom rail only. A shade is
*moving* while a tracked axis's target and actual differ. Our own commands
are tracked as *pending* targets for a short grace period, until the hub's
reported target reflects them; whether the hub ever did reflect a command is
remembered per axis, because that is what later tells a lost command apart
from a shade someone else re-commanded.

Data flow: a cover entity calls :meth:`ShadeAutoCoordinator.register_command`,
sends the command through :class:`ShadeAutoApi`, calls
:meth:`ShadeAutoCoordinator.command_sent` once the post is answered and
refreshes the coordinator (or calls :meth:`ShadeAutoCoordinator.discard_command`
if the post failed). The post can wait several ``send_spacing_sec`` for its
slot behind other shades' commands, so the grace for the hub to reflect a
command and the verify delay both run from ``command_sent``, not from
registration. While any tracked axis is *unsettled* (our command pending, or
the hub's target away from its actual) the coordinator polls ``/status`` every
``motion_poll_sec`` instead of ``poll_seconds``: the hub publishes only end
states, the integration does not read its ``/notification`` event stream yet
(docs/HUB_PROTOCOL.md 2.5), and after a stop the hub reports where the shade
ended only some 35 s later. ``verify_delay_sec`` after the post it checks once
that the shade reached its target on every axis the command set, retrying the
command a single time if it did not and if nobody else has re-commanded the
shade in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ShadeAutoApi, ShadeAutoError
from .const import (
    ATTR_BATTERY,
    ATTR_BOTTOM_RAIL,
    ATTR_MIDDLE_RAIL,
    ATTR_PERIPHERAL_UID,
    ATTR_TARGET_BOTTOM_RAIL,
    ATTR_TARGET_MIDDLE_RAIL,
    AXES,
    AXIS_BOTTOM,
    AXIS_FIELDS,
    AXIS_MIDDLE,
    DOMAIN,
    KNOWN_UNSUPPORTED_MODULE_TYPES,
    MOTION_FAILSAFE_SEC,
    PENDING_GRACE_SEC,
    POSITION_TOLERANCE,
    PROFILE_AXES,
    PROFILE_BY_MODULE,
    PROFILE_BY_MODULE_TYPE,
    PROFILE_LEGACY,
)
from .options import ShadeAutoOptions
from .util import to_int

_LOGGER = logging.getLogger(__name__)

# Fields copied from each /status record into the coordinator's data. The
# module fields fill in a profile when /GetAllPeripheral lacks them; the
# firmware field is for the device page.
STATUS_FIELDS = (
    ATTR_BOTTOM_RAIL,
    ATTR_MIDDLE_RAIL,
    ATTR_TARGET_BOTTOM_RAIL,
    ATTR_TARGET_MIDDLE_RAIL,
    ATTR_BATTERY,
    "ModuleType",
    "ModuleDetail",
    "FirmwareVersion",
)


def shade_profile(module_type: int | None, module_detail: int | None) -> str:
    """The profile (``PROFILE_*`` in ``const.py``) for a hub ``ModuleType``/``ModuleDetail`` pair.

    Anything the tables do not cover is ``legacy``: lift only, as 0.3.0
    handled every shade. That includes a missing ``ModuleDetail`` on a type
    whose profile depends on the detail (32, 48, 49), because which of its
    profiles applies cannot be told.
    """
    if module_type is None:
        return PROFILE_LEGACY
    if module_detail is None:
        if any(known_type == module_type for known_type, _ in PROFILE_BY_MODULE):
            return PROFILE_LEGACY
    elif profile := PROFILE_BY_MODULE.get((module_type, module_detail)):
        return profile
    return PROFILE_BY_MODULE_TYPE.get(module_type, PROFILE_LEGACY)


@dataclass(frozen=True, slots=True)
class Peripheral:
    """Static facts about one shade, from ``/GetAllPeripheral`` (module pair completed from ``/status``)."""

    uid: str
    name: str
    module_type: int | None
    module_detail: int | None

    @property
    def profile(self) -> str:
        """How the shade is driven and tracked; see :func:`shade_profile`."""
        return shade_profile(self.module_type, self.module_detail)

    @classmethod
    def from_hub(cls, record: dict[str, Any]) -> Peripheral:
        uid = str(record.get(ATTR_PERIPHERAL_UID))
        # PeripheralName is what the hub sends; Name is a fallback for hub firmware that might use it instead.
        name = record.get("PeripheralName") or record.get("Name")
        return cls(
            uid=uid,
            name=str(name) if name else f"Shade {uid}",
            module_type=to_int(record.get("ModuleType")),
            module_detail=to_int(record.get("ModuleDetail")),
        )


@dataclass
class AxisState:
    """What we know about one axis of one shade."""

    actual: int | None = None  # last position the hub reported
    target: int | None = None  # last target the hub reported
    pending: int | None = None  # target we sent that the hub has not reflected yet
    pending_ts: float | None = None  # monotonic time the pending target's post was answered; None while it is queued
    changed_ts: float | None = None  # monotonic time actual/target last changed (or we sent a command)
    reflected: bool = False  # the hub's target matched our last command at least once

    def at(self, value: int) -> bool:
        return self.actual is not None and abs(self.actual - value) <= POSITION_TOLERANCE


@dataclass
class ShadeState:
    """Motion bookkeeping for one shade."""

    axes: dict[str, AxisState] = field(default_factory=lambda: {axis: AxisState() for axis in AXES})
    cmd_id: int | None = None  # newest command we sent, so stale verifies can bail
    command: tuple[int, int | None] | None = None  # (bottom, middle) of that command, for its verify
    retry_attempted: bool = False
    stopped_ts: float | None = None  # monotonic time of our last stop command
    cancel_verify: CALLBACK_TYPE | None = None  # pending verify timer, if any


class ShadeAutoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the hub and keeps the per-shade status that entities render."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: ShadeAutoApi, options: ShadeAutoOptions) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {api.host}",
            update_interval=timedelta(seconds=options.poll_seconds),
        )
        self.api = api
        self.options = options
        # Assigned by async_setup_entry in the package __init__.py once the hub device exists.
        self.hub_device_id: str | None = None
        self.peripherals: dict[str, Peripheral] = {}
        self._profiles_final = False  # set once the first /status has completed the module pairs
        self._shades: dict[str, ShadeState] = {}
        self._cmd_seq = 0
        self._idle_interval = timedelta(seconds=options.poll_seconds)
        self._motion_interval = timedelta(seconds=options.motion_poll_sec)

    # --- lookups used by entities -------------------------------------------

    @property
    def identity(self) -> str:
        """Stable hub identity for unique IDs: its ThingName, else the host."""
        return self.api.thing_name or self.api.host

    def status_for(self, uid: str) -> dict[str, Any]:
        """The most recent ``/status`` record for ``uid`` (empty if unknown)."""
        status: dict[str, dict[str, Any]] = (self.data or {}).get("status", {})
        return status.get(str(uid), {})

    def _shade(self, uid: str) -> ShadeState:
        return self._shades.setdefault(str(uid), ShadeState())

    def axes_for(self, uid: str) -> tuple[str, ...]:
        """The axes tracked for ``uid``: its profile's, or the bottom rail alone for a shade setup did not list."""
        peripheral = self.peripherals.get(str(uid))
        return PROFILE_AXES[peripheral.profile if peripheral is not None else PROFILE_LEGACY]

    def _axis(self, uid: str, axis: str) -> AxisState:
        return self._shade(uid).axes[axis]

    def get_position(self, uid: str, axis: str) -> int | None:
        """The hub's last reported position on ``axis``."""
        return self._axis(uid, axis).actual

    def get_target(self, uid: str, axis: str) -> int | None:
        """Where ``axis`` is heading: our pending target, else the hub's target."""
        state = self._axis(uid, axis)
        return state.pending if state.pending is not None else state.target

    def get_held_position(self, uid: str, axis: str) -> int | None:
        """Where ``axis`` will come to rest if no new command moves it, or ``None`` if that is not known.

        For commands that must restate a rail they do not move (the hub sets
        every rail a command leaves out). That is our pending target, else the
        hub's target, else its actual position. After our stop it is unknown
        until the hub reports where the axis stopped: the hub keeps the
        abandoned target until then (about 35 s, log C4b), and restating it
        would restart the move the stop ended. kedube/ha-norman saw exactly
        that on a two-rail shade (docs/NORMAN_API.md, ``/control``).
        """
        shade = self._shade(uid)
        state = shade.axes[axis]
        if state.pending is not None:
            return state.pending
        if self._stopped_since_last_change(shade, state) and self._unsettled(uid, axis):
            return None
        return state.target if state.target is not None else state.actual

    @staticmethod
    def _stopped_since_last_change(shade: ShadeState, state: AxisState) -> bool:
        """True while the hub has reported nothing new for this axis since our stop."""
        return shade.stopped_ts is not None and (state.changed_ts is None or state.changed_ts <= shade.stopped_ts)

    def _unsettled(self, uid: str, axis: str) -> bool:
        """True while the hub still owes an end state for ``axis``.

        That is: our command is pending, or the hub's target differs from its
        actual position and the shade has not stalled. Unlike :meth:`is_moving`
        this ignores our own stop command, because the hub keeps the old target
        until it reports the stop point some 35 s later, and we must keep
        polling fast to see it.
        """
        state = self._axis(uid, axis)
        if state.pending is not None:
            return True
        if state.actual is None or state.target is None:
            return False
        if abs(state.target - state.actual) <= POSITION_TOLERANCE:
            return False
        # Nothing changing for a long time means a stalled or unreachable shade, not a move.
        stalled = state.changed_ts is not None and time.monotonic() - state.changed_ts > MOTION_FAILSAFE_SEC
        return not stalled

    def _any_unsettled(self) -> bool:
        return any(self._unsettled(uid, axis) for uid in self._shades for axis in self.axes_for(uid))

    def is_moving(self, uid: str, axis: str) -> bool:
        """True while the hub's target differs from its actual position (or our command is pending).

        Drives the entity's opening/closing flags, so a shade we just stopped
        counts as not moving even though the hub still shows the old target.
        """
        shade = self._shade(uid)
        state = shade.axes[axis]
        if state.pending is not None:
            return True
        if self._stopped_since_last_change(shade, state):
            # We stopped it; the hub re-targets to the stopped position only after a delay.
            return False
        return self._unsettled(uid, axis)

    def is_shade_moving(self, uid: str) -> bool:
        return any(self.is_moving(uid, axis) for axis in self.axes_for(uid))

    def any_moving(self) -> bool:
        return any(self.is_shade_moving(uid) for uid in self._shades)

    # --- polling --------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Register with the hub and fetch the shade list, once, before the first refresh.

        Raising ``UpdateFailed`` here makes the base class turn the failure
        into ``ConfigEntryNotReady``, so an unreachable hub puts the entry into
        setup retry instead of error.
        """
        try:
            await self.api.registration()
            records = await self.api.get_all_peripheral()
        except ShadeAutoError as err:
            raise UpdateFailed(f"Cannot reach ShadeAuto hub at {self.api.host}: {err}") from err
        self.peripherals = {p.uid: p for p in map(Peripheral.from_hub, records)}

    def _finalise_profiles(self, status: list[dict[str, Any]]) -> None:
        """Complete each shade's module pair from its first ``/status`` record, then fix its profile.

        Runs once, on the refresh that ends setup: the platforms create their
        entities from the profiles right after it, so a profile must not change
        later. Shades left on the legacy profile are logged here, once per
        setup: a warning for a pair nobody has reported yet, a debug line for
        the families the vendor app knows but this integration does not drive.
        """
        records = {str(item.get(ATTR_PERIPHERAL_UID)): item for item in status}
        for uid, peripheral in self.peripherals.items():
            record = records.get(uid, {})
            if peripheral.module_type is None:
                peripheral = replace(peripheral, module_type=to_int(record.get("ModuleType")))
            if peripheral.module_detail is None:
                peripheral = replace(peripheral, module_detail=to_int(record.get("ModuleDetail")))
            self.peripherals[uid] = peripheral
        self._profiles_final = True

        unrecognised: dict[tuple[int | None, int | None], list[str]] = {}
        for peripheral in self.peripherals.values():
            if peripheral.profile == PROFILE_LEGACY:
                unrecognised.setdefault((peripheral.module_type, peripheral.module_detail), []).append(peripheral.name)
        for (module_type, module_detail), names in unrecognised.items():
            family = KNOWN_UNSUPPORTED_MODULE_TYPES.get(module_type) if module_type is not None else None
            if family is not None:
                _LOGGER.debug(
                    "%s (%s, ModuleType %s / ModuleDetail %s) has no verified behaviour; handled as a lift-only shade",
                    ", ".join(names),
                    family,
                    module_type,
                    module_detail,
                )
                continue
            _LOGGER.warning(
                "ModuleType %s / ModuleDetail %s, reported for shade %s, is not a shade type this integration "
                "recognises, so it is handled as a lift-only shade. Please report the pair and the shade's "
                "product name in a GitHub issue so it can be supported",
                module_type,
                module_detail,
                ", ".join(names),
            )

    async def async_shutdown(self) -> None:
        for shade in self._shades.values():
            self._cancel_verify(shade)
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.api.status()
        except ShadeAutoError as err:
            raise UpdateFailed(f"status error: {err}") from err
        if not self._profiles_final:
            self._finalise_profiles(status)
        data = {"status": self._build_status_by_uid(status)}
        self._apply_poll_interval()
        return data

    def _build_status_by_uid(self, status: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Key the hub's status list by UID and feed positions into motion tracking."""
        by_uid: dict[str, dict[str, Any]] = {}
        for item in status:
            uid = str(item.get(ATTR_PERIPHERAL_UID) or "")
            if not uid:
                continue
            record = by_uid.setdefault(uid, {})
            for key in STATUS_FIELDS:
                if key in item:
                    record[key] = item[key]
            for axis in self.axes_for(uid):
                actual_key, target_key = AXIS_FIELDS[axis]
                self._update_axis(uid, axis, to_int(record.get(actual_key)), to_int(record.get(target_key)))
        return by_uid

    def _update_axis(self, uid: str, axis: str, actual: int | None, target: int | None) -> None:
        """Record what the hub reports for one axis and reconcile it with our pending command."""
        state = self._axis(uid, axis)
        now = time.monotonic()
        if actual != state.actual or target != state.target:
            state.changed_ts = now
        state.actual, state.target = actual, target
        if state.pending is None:
            return
        if target == state.pending:
            state.pending = None  # the hub accepted our command
            state.reflected = True
        elif state.pending_ts is not None and now - state.pending_ts > PENDING_GRACE_SEC:
            _LOGGER.debug("UID %s %s: hub never reflected target %s; dropping pending", uid, axis, state.pending)
            state.pending = None

    def _apply_poll_interval(self) -> None:
        """Poll fast while any axis is unsettled, slowly otherwise."""
        wanted = self._motion_interval if self._any_unsettled() else self._idle_interval
        if self.update_interval != wanted:
            _LOGGER.debug("Poll interval -> %ss", wanted.total_seconds())
            self.update_interval = wanted

    # --- commands -------------------------------------------------------------

    def register_command(self, uid: str, *, bottom: int, middle: int | None) -> int:
        """Record a move we are about to send; returns its command id.

        Call it before the ``/control`` post so the pending target covers the
        wait for the send slot and the post itself. Once the post is answered,
        hand the returned id to :meth:`command_sent` and refresh the
        coordinator so the next poll sees the hub's new target; if the post
        fails, hand it to :meth:`discard_command` instead. Pass exactly the
        fields the post carries: a command with ``middle`` sets both targets
        on the hub (the entity resolves the vane or rail value first), and
        ``middle=None`` is the bottom-only command of a shade whose middle
        axis is not tracked.
        """
        uid = str(uid)
        shade = self._shade(uid)
        self._cmd_seq += 1
        cmd_id = shade.cmd_id = self._cmd_seq
        shade.retry_attempted = False
        shade.stopped_ts = None
        bottom = int(bottom)
        middle = None if middle is None else int(middle)
        shade.command = (bottom, middle)
        self._set_pending(shade, bottom=bottom, middle=middle, sent=False)
        self._cancel_verify(shade)
        self._apply_poll_interval()
        return cmd_id

    def command_sent(self, uid: str, cmd_id: int) -> None:
        """Start the clocks of a registered command whose ``/control`` post the hub has answered.

        The grace for the hub to reflect the command (``PENDING_GRACE_SEC``)
        and the verify delay run from here, not from :meth:`register_command`:
        the API sends one command at a time, ``send_spacing_sec`` apart, so a
        command in a scene can wait longer than the grace for its slot (eight
        shades at the default 1.5 s, five at the 3 s maximum), and timing it
        from registration would drop its pending target before it was even
        sent. A no-op unless ``cmd_id`` is still the shade's newest command.
        """
        uid = str(uid)
        shade = self._shade(uid)
        if shade.cmd_id != cmd_id or shade.command is None:
            return
        now = time.monotonic()
        for state in shade.axes.values():
            if state.pending is not None:
                state.pending_ts = now
        bottom, middle = shade.command
        if self.options.verify_enabled and self.options.verify_delay_sec > 0:

            @callback
            def _start_verify(_now: datetime) -> None:
                shade.cancel_verify = None
                if self.config_entry.state is not ConfigEntryState.LOADED:
                    return
                # As an entry background task the verify dies with the entry
                # instead of posting to the hub from a half-unloaded integration.
                self.config_entry.async_create_background_task(
                    self.hass,
                    self.async_verify_and_retry(uid, bottom=bottom, middle=middle, cmd_id=cmd_id),
                    name=f"{DOMAIN} verify {uid}",
                )

            shade.cancel_verify = async_call_later(self.hass, self.options.verify_delay_sec, _start_verify)

    def discard_command(self, uid: str, cmd_id: int) -> None:
        """Forget a registered command that never reached the hub.

        Acts only while ``cmd_id`` is still the shade's newest command: clears
        the pending targets on both axes, cancels the verify timer, bumps the
        command id so nothing stale can fire, and re-applies the poll
        interval. A no-op for any older id.
        """
        shade = self._shade(uid)
        if shade.cmd_id != cmd_id:
            return
        self._cmd_seq += 1
        shade.cmd_id = self._cmd_seq
        for state in shade.axes.values():
            state.pending = None
        self._cancel_verify(shade)
        self._apply_poll_interval()

    def register_stop(self, uid: str) -> None:
        """Record that we told ``uid`` to stop; cancels any pending verify."""
        shade = self._shade(uid)
        self._cmd_seq += 1
        shade.cmd_id = self._cmd_seq
        shade.stopped_ts = time.monotonic()
        for state in shade.axes.values():
            state.pending = None
        self._cancel_verify(shade)
        self._apply_poll_interval()

    @staticmethod
    def _set_pending(shade: ShadeState, *, bottom: int, middle: int | None, sent: bool) -> None:
        """Mark the command's targets pending; the grace starts now if its post has already been answered."""
        now = time.monotonic()
        for axis, value in ((AXIS_BOTTOM, bottom), (AXIS_MIDDLE, middle)):
            if value is None:
                continue
            state = shade.axes[axis]
            state.pending = value
            state.pending_ts = now if sent else None
            state.changed_ts = now
            state.reflected = False

    @staticmethod
    def _cancel_verify(shade: ShadeState) -> None:
        if shade.cancel_verify is not None:
            shade.cancel_verify()
            shade.cancel_verify = None

    async def async_verify_and_retry(self, uid: str, *, bottom: int, middle: int | None, cmd_id: int) -> None:
        """Resend the command once if the shade is not where we asked.

        Runs ``verify_delay_sec`` after a command's post was answered and
        checks the axes the command set (``middle=None``: the bottom rail
        only). Does nothing if a newer command was issued for the shade, or if
        the hub's target on one of those axes has moved away from ours after
        once reflecting it (the app or a remote re-commanded the shade, and
        fighting them would be wrong). A target the hub never reflected means
        the command was lost, which is exactly what the retry is for.
        """
        uid = str(uid)
        shade = self._shade(uid)
        if shade.cmd_id != cmd_id or shade.retry_attempted:
            return
        await self.async_refresh()
        if shade.cmd_id != cmd_id:
            return

        wanted = {AXIS_BOTTOM: int(bottom)}
        if middle is not None:
            wanted[AXIS_MIDDLE] = int(middle)
        # An axis the hub does not report (actual None) cannot fail to arrive.
        if all(shade.axes[axis].actual is None or shade.axes[axis].at(value) for axis, value in wanted.items()):
            return
        for axis, value in wanted.items():
            state = shade.axes[axis]
            if state.reflected and state.target is not None and state.target != value:
                _LOGGER.debug("UID %s %s: target changed to %s by someone else; not retrying", uid, axis, state.target)
                return

        _LOGGER.debug(
            "UID %s not at bottom=%s middle=%s (hub reports %s/%s); retrying once",
            uid,
            bottom,
            middle,
            shade.axes[AXIS_BOTTOM].actual,
            shade.axes[AXIS_MIDDLE].actual,
        )
        shade.retry_attempted = True
        try:
            await self.api.control(uid, bottom=bottom, middle=middle)
        except ShadeAutoError as err:
            _LOGGER.warning("Retry for UID %s failed: %s", uid, err)
            return
        if shade.cmd_id != cmd_id:
            return  # a newer command arrived while the retry was in flight; its bookkeeping wins
        self._set_pending(shade, bottom=bottom, middle=middle, sent=True)
        self._apply_poll_interval()
        await self.async_refresh()
