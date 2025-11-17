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
from .const import DEFAULT_POLL, DEFAULT_NOTIFICATION_TIMEOUT, DEFAULT_VERIFY_ENABLED, DEFAULT_VERIFY_DELAY

_LOGGER = logging.getLogger(__name__)


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

    async def async_config_entry_first_refresh(self) -> None:
        await self.api.registration()
        per_list = await self.api.get_all_peripheral()
        self._peripherals = {
            str(p.get("PeripheralUID")): {
                "name": p.get("Name") or p.get("DisplayName") or f"Shade {p.get('PeripheralUID')}",
                "room_id": p.get("RoomID"),
                "module_type": p.get("ModuleType"),
                "module_detail": p.get("ModuleDetail"),
            }
            for p in per_list
        }
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
            for k in ("BottomRailPosition", "BatteryVoltage", "Name"):
                if k in item:
                    cur[k] = item[k]

            state = self._motion.setdefault(uid, ShadeMotionState())
            pos = cur.get("BottomRailPosition")
            try:
                pos_i = int(pos) if pos is not None else None
            except (TypeError, ValueError):
                pos_i = None
            state = getattr(self, "_motion", {}).get(uid)
            _LOGGER.debug(
                "pos_debug: status uid=%s raw=%s pos_i=%s state_exists=%s in_motion=%s pending=%s last_hub_pos=%s",
                uid,
                pos,
                pos_i,
                state is not None,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
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
                        "motion: UID %s hub pos %s ≈ target %s → settled",
                        uid,
                        pos_i,
                        state.pending_target,
                    )
                    state.in_motion = False
                    state.pending_target = None

        return by_uid

    def _pending_uids(self) -> list[str]:
        """Return UIDs that we still consider in-motion."""
        return [uid for uid, st in self._motion.items() if st.in_motion and st.pending_target is not None]

    def _prune_pending_with_cache(self) -> bool:
        """Use cached status to mark shades as done if they already reached their target.

        Returns True if nothing remains pending.
        """
        pending = self._pending_uids()
        if not self._peripherals or not pending:
            return not pending

        st = (self.data or {}).get("status", {}) or {}
        for uid in list(pending):
            cur = st.get(str(uid), {})
            pos = cur.get("BottomRailPosition")
            try:
                pos_i = int(pos) if pos is not None else None
            except (TypeError, ValueError):
                pos_i = None

            state = self._motion.setdefault(uid, ShadeMotionState())
            if pos_i is not None:
                state.last_hub_pos = pos_i
                if state.pending_target is not None and abs(pos_i - state.pending_target) <= 2 and state.saw_move_since_cmd:
                    self.logger.debug(
                        "cache prune: UID %s pos %s ≈ target %s → settled",
                        uid,
                        pos_i,
                        state.pending_target,
                    )
                    state.in_motion = False
                    state.pending_target = None

        return not self._pending_uids()

    def get_effective_position(self, uid: str) -> int | None:
        uid = str(uid)
        state = getattr(self, "_motion", {}).get(uid)
        raw = None
        # Try to read the cached hub value for debug purposes
        try:
            raw = (self.data or {}).get("status", {}).get(uid, {}).get("BottomRailPosition")
        except Exception:
            pass

        if state and state.in_motion and state.pending_target is not None:
            _LOGGER.debug(
                "pos_debug: get_effective_position uid=%s -> pending=%s (in_motion=True, last_hub_pos=%s, raw=%s)",
                uid,
                state.pending_target,
                getattr(state, "last_hub_pos", None),
                raw,
            )
            return state.pending_target

        if state and state.last_hub_pos is not None:
            _LOGGER.debug(
                "pos_debug: get_effective_position uid=%s -> last_hub_pos=%s (in_motion=%s, pending=%s, raw=%s)",
                uid,
                state.last_hub_pos,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                raw,
            )
            return state.last_hub_pos

        _LOGGER.debug(
            "pos_debug: get_effective_position uid=%s -> raw=%s (no state or no motion)",
            uid,
            raw,
        )
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None


    # --- command / verify / long-poll ---

    def register_command(self, uid: str, target: int) -> int:
        """Record latest command for a shade and start watcher + verify timer."""
        uid_s = str(uid)
        self._cmd_seq += 1
        cid = self._cmd_seq
        now = time.monotonic()
        target_i = int(target)
        self._last_cmd[uid_s] = {"id": cid, "target": target_i, "t": now}

        state = self._motion.get(uid_s)
        if state is None:
            state = self._motion[uid_s] = ShadeMotionState()

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
                self.async_verify_and_retry(uid_s, target_i, delay=verify_delay, cmd_id=cid)
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
        prev: int | None = None,
        moved_threshold: int = 1,
        delay: float = 20.0,
        cmd_id: int | None = None,
    ) -> None:
        """Retry once only if this command is still latest and shade isn't at target."""
        uid_s = str(uid)
        latest = self._last_cmd.get(uid_s)
        if cmd_id is not None and (latest is None or latest.get("id") != cmd_id):
            return

        await asyncio.sleep(max(0.1, delay))

        latest = self._last_cmd.get(uid_s)
        if cmd_id is not None and (latest is None or latest.get("id") != cmd_id):
            return

        # Quick check from motion state first
        state = self._motion.get(uid_s)
        if state and state.pending_target is not None and state.last_hub_pos is not None:
            if abs(state.last_hub_pos - state.pending_target) <= 2:
                return

        # Fallback to cached status for the UID
        await self.async_request_refresh()
        st = (self.data or {}).get("status", {}).get(uid_s, {})
        pos_raw = st.get("BottomRailPosition")
        try:
            pos = int(pos_raw) if pos_raw is not None else None
        except (TypeError, ValueError):
            pos = None
            state = getattr(self, "_motion", {}).get(uid_s)
            _LOGGER.debug(
                "pos_debug_1: verify uid=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
            )
        if (pos is None or abs(pos - int(target)) > 2) and latest and latest.get("id") == cmd_id:
            self.logger.debug(
                "verify_and_retry: UID %s not at target after %.1fs (pos=%s, want=%s) -> retry",
                uid_s,
                delay,
                pos,
                target,
            )
            # Mark as retried and send one more control
            state = self._motion.setdefault(uid_s, ShadeMotionState())
            state2 = getattr(self, "_motion", {}).get(uid_s)
            _LOGGER.debug(
                "pos_debug_2: verify uid=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                getattr(state2, "in_motion", None),
                getattr(state2, "pending_target", None),
                getattr(state2, "last_hub_pos", None),
            )
            state.retry_attempted = True
            state.last_control_ts = time.monotonic()
            self._last_global_control_ts = state.last_control_ts
            await self.api.control(uid_s, bottom=int(target))
            state = getattr(self, "_motion", {}).get(uid_s)
            _LOGGER.debug(
                "pos_debug_3: verify uid=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
            )
            await self.async_request_refresh()
            state = getattr(self, "_motion", {}).get(uid_s)
            _LOGGER.debug(
                "pos_debug_4: verify uid=%s state_before_retry in_motion=%s pending=%s last_hub_pos=%s",
                uid_s,
                getattr(state, "in_motion", None),
                getattr(state, "pending_target", None),
                getattr(state, "last_hub_pos", None),
            )
