from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ShadeAutoApi
from .const import (
    DEFAULT_POLL,
    DEFAULT_NOTIFICATION_TIMEOUT,
    DEFAULT_VERIFY_ENABLED,
    DEFAULT_VERIFY_DELAY,
    DEFAULT_COLLISION_GUARD,
    DEFAULT_RAIL_MIN_GAP,
    RAIL_BOTTOM,
    RAIL_MIDDLE,
    RAIL_FIELDS,
    TDBU_MODULE_TYPES,
)

_LOGGER = logging.getLogger(__name__)


def _mkey(uid: str, rail: str = RAIL_BOTTOM) -> str:
    """Motion-state key for a shade rail. Bottom keeps a stable, rail-tagged key."""
    return f"{uid}:{rail}"


def _split_key(key: str) -> tuple[str, str]:
    """Inverse of _mkey -> (uid, rail)."""
    uid, _, rail = key.rpartition(":")
    return (uid, rail or RAIL_BOTTOM)


@dataclass
class ShadeMotionState:
    """Per-shade motion / verification state."""
    last_hub_pos: int | None = None
    pending_target: int | None = None
    in_motion: bool = False
    last_control_ts: float | None = None  # monotonic()
    retry_attempted: bool = False
    start_pos: int | None = None          # hub pos at time of last command
    saw_move_since_cmd: bool = False      # have we seen a different pos since that command?


class ShadeAutoCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Polls hub status and caches peripherals + latest state."""

    def __init__(self, hass: HomeAssistant, api: ShadeAutoApi, poll_seconds: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="ShadeAuto Coordinator",
            update_interval=timedelta(seconds=int(poll_seconds) if poll_seconds else DEFAULT_POLL),
        )
        self.api = api
        self._peripherals: Dict[str, Dict[str, Any]] = {}
        self._cmd_seq = 0
        self._last_cmd: Dict[str, Dict[str, Any]] = {}
        # Rail commands carry both targets. Serialize payload construction as well
        # as the POST so concurrent cover calls cannot overwrite a sibling move.
        self._rail_command_locks: dict[str, asyncio.Lock] = {}
        # Motion / fake-position state
        self._motion: dict[str, ShadeMotionState] = {}
        # Long-poll state
        self._notif_task: asyncio.Task | None = None
        self._notif_ts: int = int(time.time())
        self._last_global_control_ts: float | None = None
        # ConfigEntry is wired in __init__.py
        self.config_entry = None

    @property
    def peripherals(self) -> Dict[str, Dict[str, Any]]:
        return self._peripherals

    def is_tdbu(self, uid: str) -> bool:
        """True if the shade exposes a second (top-down) rail, per its ModuleType."""
        meta = self._peripherals.get(str(uid), {})
        try:
            return int(meta.get("module_type")) in TDBU_MODULE_TYPES
        except (TypeError, ValueError):
            return False

    def _rails_for(self, uid: str) -> tuple[str, ...]:
        """Rails to track for this shade: bottom always, middle only when TDBU."""
        return (RAIL_BOTTOM, RAIL_MIDDLE) if self.is_tdbu(uid) else (RAIL_BOTTOM,)

    async def async_config_entry_first_refresh(self) -> None:
        await self.api.registration()
        per_list = await self.api.get_all_peripheral()
        peripherals: Dict[str, Dict[str, Any]] = {}
        for p in per_list:
            uid = str(p.get("PeripheralUID"))
            meta = peripherals.setdefault(uid, {"room_id": None, "module_type": None, "module_detail": None})
            name = p.get("Name") or p.get("DisplayName") or p.get("PeripheralName")
            if name:
                meta["name"] = name
            for src, dst in (("RoomID", "room_id"), ("ModuleType", "module_type"), ("ModuleDetail", "module_detail")):
                if p.get(src) is not None:
                    meta[dst] = p.get(src)
        for uid, meta in peripherals.items():
            meta.setdefault("name", f"Shade {uid}")
        self._peripherals = peripherals
        await super().async_config_entry_first_refresh()

    async def _async_update_data(self) -> Dict[str, Any]:
        try:
            status = await self.api.status()
        except Exception as err:  # pragma: no cover - transport errors are logged by HA
            raise UpdateFailed(f"status error: {err}") from err

        by_uid = self._build_status_by_uid(status)
        return {"thing_name": self.api.thing_name, "peripherals": self._peripherals, "status": by_uid}

    # --- motion helpers ---

    def _build_status_by_uid(self, status: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Normalise hub status list into a dict keyed by UID and update motion state."""
        by_uid: Dict[str, Dict[str, Any]] = {}
        for item in status:
            uid = str(item.get("PeripheralUID") or "")
            if not uid:
                continue
            cur = by_uid.setdefault(uid, {})
            for k in ("BottomRailPosition", "MiddleRailPosition", "BatteryVoltage", "Name"):
                if k in item:
                    cur[k] = item[k]

            # Update motion/settlement for each rail this shade actually has.
            for rail in self._rails_for(uid):
                pos = cur.get(RAIL_FIELDS[rail])
                try:
                    pos_i = int(pos) if pos is not None else None
                except (TypeError, ValueError):
                    pos_i = None
                self._update_rail_motion(uid, rail, pos, pos_i)

        return by_uid

    def _update_rail_motion(self, uid: str, rail: str, pos: Any, pos_i: int | None) -> None:
        """Advance motion/settlement bookkeeping for one (uid, rail)."""
        key = _mkey(uid, rail)
        state = self._motion.setdefault(key, ShadeMotionState())
        _LOGGER.debug(
            "pos_debug: status uid=%s rail=%s raw=%s pos_i=%s in_motion=%s pending=%s last_hub_pos=%s",
            uid,
            rail,
            pos,
            pos_i,
            state.in_motion,
            state.pending_target,
            state.last_hub_pos,
        )
        state.last_hub_pos = pos_i

        # If we have a baseline for this command and we've seen a different pos,
        # remember that we've observed actual motion since the command started.
        if (
            state.start_pos is not None
            and pos_i is not None
            and abs(pos_i - state.start_pos) > 2
        ):
            state.saw_move_since_cmd = True

        if state.in_motion and state.pending_target is not None and pos_i is not None:
            if abs(pos_i - state.pending_target) <= 2 and state.saw_move_since_cmd:
                self.logger.debug(
                    "motion: UID %s rail %s hub pos %s ≈ target %s → settled",
                    uid,
                    rail,
                    pos_i,
                    state.pending_target,
                )
                state.in_motion = False
                state.pending_target = None

    def _pending_keys(self) -> list[str]:
        """Return (uid:rail) motion keys we still consider in-motion."""
        return [k for k, st in self._motion.items() if st.in_motion and st.pending_target is not None]

    def _pending_uids(self) -> list[str]:
        """Return bare UIDs still in motion (deduped across rails).

        The long-poll watcher matches these against the hub's PeripheralList, which is
        keyed per shade, not per rail — so this must yield bare UIDs.
        """
        seen: list[str] = []
        for k in self._pending_keys():
            uid = _split_key(k)[0]
            if uid not in seen:
                seen.append(uid)
        return seen

    def _prune_pending_with_cache(self) -> bool:
        """Use cached status to mark rails as done if they already reached their target.

        Returns True if nothing remains pending.
        """
        pending = self._pending_keys()
        if not self._peripherals or not pending:
            return not pending

        st = (self.data or {}).get("status", {}) or {}
        for key in list(pending):
            uid, rail = _split_key(key)
            cur = st.get(str(uid), {})
            pos = cur.get(RAIL_FIELDS[rail])
            try:
                pos_i = int(pos) if pos is not None else None
            except (TypeError, ValueError):
                pos_i = None

            state = self._motion.setdefault(key, ShadeMotionState())
            if pos_i is not None:
                state.last_hub_pos = pos_i
                if state.pending_target is not None and abs(pos_i - state.pending_target) <= 2 and state.saw_move_since_cmd:
                    self.logger.debug(
                        "cache prune: UID %s rail %s pos %s ≈ target %s → settled",
                        uid,
                        rail,
                        pos_i,
                        state.pending_target,
                    )
                    state.in_motion = False
                    state.pending_target = None

        return not self._pending_keys()

    def get_effective_position(self, uid: str, rail: str = RAIL_BOTTOM) -> int | None:
        uid = str(uid)
        state = getattr(self, "_motion", {}).get(_mkey(uid, rail))
        raw = None
        # Try to read the cached hub value for debug purposes
        try:
            raw = (self.data or {}).get("status", {}).get(uid, {}).get(RAIL_FIELDS[rail])
        except Exception:
            pass

        if state and state.in_motion and state.pending_target is not None:
            _LOGGER.debug(
                "pos_debug: get_effective_position uid=%s rail=%s -> pending=%s (in_motion=True, last_hub_pos=%s, raw=%s)",
                uid,
                rail,
                state.pending_target,
                getattr(state, "last_hub_pos", None),
                raw,
            )
            return state.pending_target

        if state and state.last_hub_pos is not None:
            _LOGGER.debug(
                "pos_debug: get_effective_position uid=%s rail=%s -> last_hub_pos=%s (in_motion=%s, pending=%s, raw=%s)",
                uid,
                rail,
                state.last_hub_pos,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                raw,
            )
            return state.last_hub_pos

        _LOGGER.debug(
            "pos_debug: get_effective_position uid=%s rail=%s -> raw=%s (no state or no motion)",
            uid,
            rail,
            raw,
        )
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None


    # --- command / verify / long-poll ---

    def _collision_config(self) -> tuple[bool, int]:
        """Read collision-guard settings from options (or defaults)."""
        ce = getattr(self, "config_entry", None)
        guard = DEFAULT_COLLISION_GUARD
        gap = DEFAULT_RAIL_MIN_GAP
        try:
            if ce is not None:
                guard = bool(ce.options.get("collision_guard", DEFAULT_COLLISION_GUARD))
                gap = int(ce.options.get("rail_min_gap", DEFAULT_RAIL_MIN_GAP))
        except Exception:
            pass
        return guard, gap

    def clamp_target(self, uid: str, rail: str, target: int) -> int:
        """Clamp a rail move so the two rails of a TDBU shade cannot cross.

        Invariant: the middle (top-down) rail stays at or above the bottom rail on the
        hub's position axis, i.e. ``middle >= bottom + gap``. Only applied to TDBU
        shades; single-rail shades are returned unchanged. Direction confirmed against
        real hardware (see README). If a legitimate move is ever blocked, disable the
        guard via options or flip this comparison.
        """
        target_i = int(target)
        guard, gap = self._collision_config()
        if not guard or not self.is_tdbu(uid):
            return target_i

        other = RAIL_MIDDLE if rail == RAIL_BOTTOM else RAIL_BOTTOM
        other_pos = self.get_effective_position(uid, other)
        if other_pos is None:
            return target_i

        if rail == RAIL_MIDDLE:
            lo = int(other_pos) + gap          # middle must stay >= bottom + gap
            clamped = max(target_i, lo)
        else:
            hi = int(other_pos) - gap          # bottom must stay <= middle - gap
            clamped = min(target_i, hi)
        clamped = max(0, min(100, clamped))

        if clamped != target_i:
            self.logger.debug(
                "collision guard: UID %s rail %s target %s -> %s (other rail %s at %s, gap %s)",
                uid, rail, target_i, clamped, other, other_pos, gap,
            )
        return clamped

    async def async_send_rail(self, uid: str, rail: str, target: int) -> None:
        """POST a rail move to the hub.

        TDBU shades reject a middle-only /control and treat a command as operating on
        both rails at once (a bottom-only payload implies MiddleRailPosition=0). So for
        TDBU shades we always include the other rail's current position to hold it in
        place; single-rail shades keep the original bottom-only payload.
        """
        kwargs: Dict[str, int] = {rail: int(target)}
        if self.is_tdbu(uid):
            other = RAIL_MIDDLE if rail == RAIL_BOTTOM else RAIL_BOTTOM
            hold = self.get_effective_position(uid, other)
            if hold is not None:
                kwargs[other] = int(hold)
        await self.api.control(uid, **kwargs)

    async def async_move_rail(self, uid: str, rail: str, target: int) -> int:
        """Send a safe rail pair without discarding the requested destination.

        If the requested rail would cross its companion, move the companion just
        enough to make room. This lets both rails reach the top regardless of the
        order in which a scene calls their cover entities.
        """
        uid = str(uid)
        lock = self._rail_command_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            pos = max(0, min(100, int(target)))
            targets = {rail: pos}
            other = RAIL_MIDDLE if rail == RAIL_BOTTOM else RAIL_BOTTOM
            hold = None
            if self.is_tdbu(uid):
                hold = self.get_effective_position(uid, other)
                if hold is None:
                    raise UpdateFailed("Cannot move TDBU shade without both rail positions")
                guard, gap = self._collision_config()
                if guard:
                    gap = max(0, min(100, gap))
                    if rail == RAIL_BOTTOM:
                        pos = min(pos, 100 - gap)
                        targets[other] = max(hold, pos + gap)
                    else:
                        pos = max(pos, gap)
                        targets[other] = min(hold, pos - gap)
                    targets[rail] = pos
                else:
                    targets[other] = hold
            await self.api.control(uid, **targets)
            for moved_rail, moved_target in targets.items():
                if moved_rail == rail or moved_target != hold:
                    self.register_command(uid, moved_target, rail=moved_rail)
            self.async_update_listeners()
            return pos

    async def _async_retry_rail(self, uid: str, rail: str, target: int, cmd_id: int) -> None:
        """Keep retries from racing new moves or resurrecting superseded targets."""
        lock = self._rail_command_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            latest = self._last_cmd.get(_mkey(uid, rail))
            if latest is None or latest.get("id") != cmd_id:
                return
            retry_target = self.clamp_target(uid, rail, target)
            await self.async_send_rail(uid, rail, retry_target)

    def register_command(self, uid: str, target: int, rail: str = RAIL_BOTTOM) -> int:
        """Record latest command for a shade rail and start watcher + verify timer."""
        uid_s = str(uid)
        key = _mkey(uid_s, rail)
        self._cmd_seq += 1
        cid = self._cmd_seq
        now = time.monotonic()
        target_i = int(target)
        self._last_cmd[key] = {"id": cid, "target": target_i, "t": now}

        state = self._motion.get(key)
        if state is None:
            state = self._motion[key] = ShadeMotionState()

        # Baseline for this command: where did the hub think we were when we started?
        state.start_pos = state.last_hub_pos
        state.saw_move_since_cmd = False
        
        state.in_motion = True
        state.pending_target = target_i
        state.last_control_ts = now
        state.retry_attempted = False
        self._last_global_control_ts = now

        # Start long-poll watcher once per hub
        if self._notif_task is None or self._notif_task.done():
            self._notif_task = self.hass.async_create_task(self._notification_watch())

        # Schedule verify/retry if enabled
        ce = getattr(self, "config_entry", None)
        verify_enabled = DEFAULT_VERIFY_ENABLED
        verify_delay = DEFAULT_VERIFY_DELAY
        try:
            if ce is not None:
                opts = ce.options
                verify_enabled = bool(opts.get("verify_enabled", DEFAULT_VERIFY_ENABLED))
                verify_delay = float(opts.get("verify_delay_sec", DEFAULT_VERIFY_DELAY))
        except Exception:
            pass

        if verify_enabled and verify_delay > 0:
            self.hass.async_create_task(
                self.async_verify_and_retry(uid_s, target_i, rail=rail, delay=verify_delay, cmd_id=cid)
            )

        return cid

    async def _notification_watch(self) -> None:
        """Run long-poll cycles until all moving shades reach their targets or timeout."""
        try:
            spacing = getattr(self.api, "send_spacing_sec", 0.5)
            # Small arming delay so both /control posts go out before any status read
            await asyncio.sleep(float(spacing) + 0.2)

            while self._pending_uids():
                # 2-minute failsafe from last control
                now_mono = time.monotonic()
                if (
                    self._last_global_control_ts is not None
                    and now_mono - self._last_global_control_ts > 120.0
                ):
                    self.logger.warning(
                        "long-poll: >120s since last control; clearing motion state and stopping watcher"
                    )
                    for st in self._motion.values():
                        if st.in_motion:
                            st.in_motion = False
                            st.pending_target = None
                    # Force a fresh status so UI matches the hub
                    await self.async_request_refresh()
                    break

                # If the cached status already shows we're done (e.g. due to base 30s poll), exit.
                if self._prune_pending_with_cache():
                    self.logger.debug("long-poll: pending cleared per cached status, stopping")
                    break

                ce = getattr(self, "config_entry", None)
                try:
                    timeout = float(
                        ce.options.get("notification_timeout_sec", DEFAULT_NOTIFICATION_TIMEOUT)
                    ) if ce else float(DEFAULT_NOTIFICATION_TIMEOUT)
                except Exception:
                    timeout = float(DEFAULT_NOTIFICATION_TIMEOUT)

                # Make sure our watermark doesn’t go backwards
                now_s = int(time.time())
                if self._notif_ts < now_s - 1:
                    self._notif_ts = now_s

                self.logger.debug("long-poll: hold %.2fs from ts=%s", timeout, self._notif_ts)

                try:
                    raw = await self.api.notification(timestamp=self._notif_ts, timeout=timeout)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception(
                        "long-poll: notification() failed; will retry while any shade is pending"
                    )
                    await asyncio.sleep(1.0)
                    continue

                if isinstance(raw, str) and raw.strip():
                    text = raw.strip()
                    for chunk in re.split(r"}\s*{", text):
                        if not chunk:
                            continue
                        if not chunk.startswith("{"):
                            chunk = "{" + chunk
                        if not chunk.endswith("}"):
                            chunk = chunk + "}"
                        try:
                            obj = json.loads(chunk)
                        except Exception:
                            continue

                        # advance watermark
                        ts_val = obj.get("Status") or obj.get("Timestamp")
                        if isinstance(ts_val, (int, float)):
                            if ts_val > 10_000_000_000:
                                ts_val = int(ts_val / 1000)
                            self._notif_ts = int(ts_val)

                        # handle events
                        plist = obj.get("PeripheralList")
                        if isinstance(plist, list) and plist:
                            pending = set(self._pending_uids())
                            if any(str(uid) in pending for uid in plist):
                                # Reread full status and update cache + motion state
                                status = await self.api.status()
                                by_uid = self._build_status_by_uid(status)
                                # Push into coordinator data for immediate UI update
                                current = dict(self.data or {})
                                current.setdefault("thing_name", self.api.thing_name)
                                current.setdefault("peripherals", self._peripherals)
                                current["status"] = by_uid
                                self.async_set_updated_data(current)
                # Normal: loop again until pending clears or failsafe fires

            self.logger.info("long-poll: all commanded shades settled; stopping watcher")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("long-poll watcher error")
        finally:
            self._notif_task = None

    async def async_verify_and_retry(
        self,
        uid: str,
        target: int,
        *,
        rail: str = RAIL_BOTTOM,
        prev: int | None = None,
        moved_threshold: int = 1,
        delay: float = 20.0,
        cmd_id: int | None = None,
    ) -> None:
        """Retry once only if this command is still latest and shade isn't at target."""
        uid_s = str(uid)
        key = _mkey(uid_s, rail)
        latest = self._last_cmd.get(key)
        if cmd_id is not None and (latest is None or latest.get("id") != cmd_id):
            return

        await asyncio.sleep(max(0.1, delay))

        latest = self._last_cmd.get(key)
        if cmd_id is not None and (latest is None or latest.get("id") != cmd_id):
            return

        # Quick check from motion state first
        state = self._motion.get(key)
        if state and state.pending_target is not None and state.last_hub_pos is not None:
            if abs(state.last_hub_pos - state.pending_target) <= 2:
                return

        # Fallback to cached status for the UID
        await self.async_request_refresh()
        st = (self.data or {}).get("status", {}).get(uid_s, {})
        pos_raw = st.get(RAIL_FIELDS[rail])
        try:
            pos = int(pos_raw) if pos_raw is not None else None
        except (TypeError, ValueError):
            pos = None
            state = getattr(self, "_motion", {}).get(key)
            _LOGGER.debug(
                "pos_debug_1: verify uid=%s rail=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                rail,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
            )
        if (pos is None or abs(pos - int(target)) > 2) and latest and latest.get("id") == cmd_id:
            self.logger.debug(
                "verify_and_retry: UID %s rail %s not at target after %.1fs (pos=%s, want=%s) -> retry",
                uid_s,
                rail,
                delay,
                pos,
                target,
            )
            # Mark as retried and send one more control (re-clamped for safety)
            state = self._motion.setdefault(key, ShadeMotionState())
            state2 = getattr(self, "_motion", {}).get(key)
            _LOGGER.debug(
                "pos_debug_2: verify uid=%s rail=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                rail,
                getattr(state2, "in_motion", None),
                getattr(state2, "pending_target", None),
                getattr(state2, "last_hub_pos", None),
            )
            state.retry_attempted = True
            state.last_control_ts = time.monotonic()
            self._last_global_control_ts = state.last_control_ts
            await self._async_retry_rail(uid_s, rail, int(target), cmd_id)
            state = getattr(self, "_motion", {}).get(key)
            _LOGGER.debug(
                "pos_debug_3: verify uid=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
            )
            await self.async_request_refresh()
            state = getattr(self, "_motion", {}).get(key)
            _LOGGER.debug(
                "pos_debug_4: verify uid=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
            )
