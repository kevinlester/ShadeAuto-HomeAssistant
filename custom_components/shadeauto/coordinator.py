"""Polling coordinator: fetches hub status and tracks commanded moves.

Data flow: a cover entity sends a command through :class:`ShadeAutoApi` and
then calls :meth:`ShadeAutoCoordinator.register_command`. The coordinator polls
``/status`` on a fixed interval, re-reads it early whenever the hub's long-poll
notification names a shade we are waiting for, and after ``verify_delay_sec``
checks once that the shade reached its target, retrying the command a single
time if it did not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ShadeAutoApi, ShadeAutoError
from .const import (
    ATTR_BATTERY,
    ATTR_BOTTOM_RAIL,
    ATTR_PERIPHERAL_UID,
    DOMAIN,
    MOTION_FAILSAFE_SEC,
    POSITION_TOLERANCE,
)
from .options import ShadeAutoOptions
from .util import to_int

_LOGGER = logging.getLogger(__name__)

# Fields copied from each /status record into the coordinator's data.
STATUS_FIELDS = (ATTR_BOTTOM_RAIL, ATTR_BATTERY, "Name")

# A notification body may hold several JSON objects back to back.
_JSON_OBJECT_BOUNDARY = re.compile(r"}\s*{")
# Timestamps above this value are milliseconds rather than seconds.
_MILLISECOND_THRESHOLD = 10_000_000_000


@dataclass(frozen=True, slots=True)
class Peripheral:
    """Static facts about one shade, from ``/GetAllPeripheral``."""

    uid: str
    name: str
    module_type: int | None
    module_detail: int | None

    @classmethod
    def from_hub(cls, record: dict[str, Any]) -> Peripheral:
        uid = str(record.get(ATTR_PERIPHERAL_UID))
        return cls(
            uid=uid,
            name=record.get("Name") or record.get("DisplayName") or f"Shade {uid}",
            module_type=to_int(record.get("ModuleType")),
            module_detail=to_int(record.get("ModuleDetail")),
        )


@dataclass
class ShadeMotionState:
    """What the coordinator knows about a shade's last commanded move."""

    last_hub_pos: int | None = None  # most recent BottomRailPosition from the hub
    pending_target: int | None = None  # position we asked for and have not seen reached
    in_motion: bool = False
    last_control_ts: float | None = None  # time.monotonic() when the last command was sent
    retry_attempted: bool = False
    start_pos: int | None = None  # hub position when the command was sent
    saw_move_since_cmd: bool = False  # the hub position has changed since the command

    def is_at_target(self, pos: int) -> bool:
        return self.pending_target is not None and abs(pos - self.pending_target) <= POSITION_TOLERANCE

    def mark_settled(self) -> None:
        self.in_motion = False
        self.pending_target = None


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
        self.hub_device_id: str | None = None  # set by __init__ once the hub device exists
        self.peripherals: dict[str, Peripheral] = {}
        self._motion: dict[str, ShadeMotionState] = {}
        self._cmd_seq = 0
        self._last_cmd_id: dict[str, int] = {}  # newest command per shade, so stale verifies can bail
        self._notif_task: asyncio.Task[None] | None = None
        self._notif_ts = int(time.time())  # watermark passed to /notification
        self._last_control_ts: float | None = None

    # --- lookups used by entities -------------------------------------------

    @property
    def identity(self) -> str:
        """Stable hub identity for unique IDs: its ThingName, else the host."""
        return self.api.thing_name or self.api.host

    def status_for(self, uid: str) -> dict[str, Any]:
        """The most recent ``/status`` record for ``uid`` (empty if unknown)."""
        status: dict[str, dict[str, Any]] = (self.data or {}).get("status", {})
        return status.get(str(uid), {})

    def get_effective_position(self, uid: str) -> int | None:
        """Position to show: the pending target while moving, else the hub's value."""
        state = self._motion.get(str(uid))
        if state and state.in_motion and state.pending_target is not None:
            return state.pending_target
        if state and state.last_hub_pos is not None:
            return state.last_hub_pos
        return to_int(self.status_for(uid).get(ATTR_BOTTOM_RAIL))

    # --- polling --------------------------------------------------------------

    async def async_config_entry_first_refresh(self) -> None:
        try:
            await self.api.registration()
            records = await self.api.get_all_peripheral()
        except ShadeAutoError as err:
            raise ConfigEntryNotReady(f"Cannot reach ShadeAuto hub at {self.api.host}: {err}") from err
        self.peripherals = {p.uid: p for p in map(Peripheral.from_hub, records)}
        await super().async_config_entry_first_refresh()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.api.status()
        except ShadeAutoError as err:
            raise UpdateFailed(f"status error: {err}") from err
        return {"status": self._build_status_by_uid(status)}

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
            self._observe_position(uid, to_int(record.get(ATTR_BOTTOM_RAIL)))
        return by_uid

    def _observe_position(self, uid: str, pos: int | None) -> None:
        """Update motion state for ``uid`` with a position reported by the hub."""
        state = self._motion.setdefault(uid, ShadeMotionState())
        state.last_hub_pos = pos
        if pos is None:
            return
        if state.start_pos is not None and abs(pos - state.start_pos) > POSITION_TOLERANCE:
            state.saw_move_since_cmd = True
        if state.in_motion and state.saw_move_since_cmd and state.is_at_target(pos):
            _LOGGER.debug("UID %s reached %s (target %s); settled", uid, pos, state.pending_target)
            state.mark_settled()

    def _pending_uids(self) -> list[str]:
        """Shades we have commanded and not yet seen arrive."""
        return [uid for uid, st in self._motion.items() if st.in_motion and st.pending_target is not None]

    def _settle_from_cache(self) -> bool:
        """Re-check pending shades against the cached status; True when none remain."""
        for uid in self._pending_uids():
            self._observe_position(uid, to_int(self.status_for(uid).get(ATTR_BOTTOM_RAIL)))
        return not self._pending_uids()

    # --- commands -------------------------------------------------------------

    def register_command(self, uid: str, target: int) -> int:
        """Record that ``uid`` was told to move to ``target``; returns the command id.

        Starts the notification watcher (once per hub) and, if enabled,
        schedules the verify-and-retry check.
        """
        uid = str(uid)
        target = int(target)
        now = time.monotonic()
        self._cmd_seq += 1
        cmd_id = self._cmd_seq
        self._last_cmd_id[uid] = cmd_id

        state = self._motion.setdefault(uid, ShadeMotionState())
        state.start_pos = state.last_hub_pos
        state.saw_move_since_cmd = False
        state.in_motion = True
        state.pending_target = target
        state.last_control_ts = now
        state.retry_attempted = False
        self._last_control_ts = now

        if self._notif_task is None or self._notif_task.done():
            self._notif_task = self.config_entry.async_create_background_task(
                self.hass, self._notification_watch(), name=f"{DOMAIN} notification watch"
            )

        if self.options.verify_enabled and self.options.verify_delay_sec > 0:
            self.config_entry.async_create_background_task(
                self.hass,
                self.async_verify_and_retry(uid, target, delay=self.options.verify_delay_sec, cmd_id=cmd_id),
                name=f"{DOMAIN} verify {uid}",
            )
        return cmd_id

    async def _notification_watch(self) -> None:
        """Long-poll the hub while any shade is pending, re-reading status on events."""
        try:
            # Let a burst of /control posts go out before the first status read.
            await asyncio.sleep(self.api.send_spacing_sec + 0.2)

            while self._pending_uids():
                if self._last_control_ts is not None and time.monotonic() - self._last_control_ts > MOTION_FAILSAFE_SEC:
                    _LOGGER.warning(
                        "No settle seen %.0fs after the last command; clearing motion state", MOTION_FAILSAFE_SEC
                    )
                    for state in self._motion.values():
                        if state.in_motion:
                            state.mark_settled()
                    await self.async_request_refresh()
                    break

                if self._settle_from_cache():
                    break

                self._notif_ts = max(self._notif_ts, int(time.time()) - 1)
                try:
                    raw = await self.api.notification(
                        timestamp=self._notif_ts, hold_sec=self.options.notification_timeout_sec
                    )
                except ShadeAutoError as err:
                    _LOGGER.debug("notification failed (%s); retrying while shades are pending", err)
                    await asyncio.sleep(1.0)
                    continue

                if raw.strip():
                    await self._handle_notification(raw.strip())
            _LOGGER.debug("All commanded shades settled; notification watch stopped")
        finally:
            self._notif_task = None

    async def _handle_notification(self, text: str) -> None:
        """Parse one or more notification objects and refresh status if they concern us."""
        for chunk in _JSON_OBJECT_BOUNDARY.split(text):
            if not chunk:
                continue
            if not chunk.startswith("{"):
                chunk = "{" + chunk
            if not chunk.endswith("}"):
                chunk = chunk + "}"
            try:
                event = json.loads(chunk)
            except ValueError:
                _LOGGER.debug("Ignoring unparsable notification chunk: %s", chunk[:200])
                continue

            ts_val = event.get("Status") or event.get("Timestamp")
            if isinstance(ts_val, (int, float)):
                if ts_val > _MILLISECOND_THRESHOLD:
                    ts_val = ts_val / 1000
                self._notif_ts = int(ts_val)

            peripherals = event.get("PeripheralList")
            if not isinstance(peripherals, list):
                continue
            pending = set(self._pending_uids())
            if any(str(uid) in pending for uid in peripherals):
                status = await self.api.status()
                self.async_set_updated_data({"status": self._build_status_by_uid(status)})

    async def async_verify_and_retry(self, uid: str, target: int, *, delay: float, cmd_id: int) -> None:
        """After ``delay``, resend the command once if ``uid`` is not at ``target``.

        Does nothing if a newer command for the same shade has been issued.
        """
        uid = str(uid)
        if self._last_cmd_id.get(uid) != cmd_id:
            return
        await asyncio.sleep(max(0.1, delay))
        if self._last_cmd_id.get(uid) != cmd_id:
            return

        state = self._motion.get(uid)
        if state and state.last_hub_pos is not None and state.is_at_target(state.last_hub_pos):
            return

        await self.async_request_refresh()
        pos = to_int(self.status_for(uid).get(ATTR_BOTTOM_RAIL))
        if pos is not None and abs(pos - target) <= POSITION_TOLERANCE:
            return
        if self._last_cmd_id.get(uid) != cmd_id:
            return

        _LOGGER.debug("UID %s not at %s after %.0fs (hub reports %s); retrying once", uid, target, delay, pos)
        state = self._motion.setdefault(uid, ShadeMotionState())
        state.retry_attempted = True
        state.last_control_ts = time.monotonic()
        self._last_control_ts = state.last_control_ts
        try:
            await self.api.control(uid, bottom=target)
        except ShadeAutoError as err:
            _LOGGER.warning("Retry for UID %s failed: %s", uid, err)
            return
        await self.async_request_refresh()
