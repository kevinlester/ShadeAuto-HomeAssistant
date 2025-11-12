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
)

_LOGGER = logging.getLogger(__name__)

# Approximate travel time: 0 -> 100 takes ~25 seconds → 0.25s per percent
UI_SECONDS_PER_PERCENT = 0.25


@dataclass
class ShadeMotionState:
    """Per-shade motion / verification and UI state."""

    last_hub_pos: int | None = None
    pending_target: int | None = None
    in_motion: bool = False
    last_control_ts: float | None = None  # monotonic()
    retry_attempted: bool = False
    # UI-only motion indicator (for opening/closing)
    ui_motion_dir: int = 0  # +1 opening, -1 closing, 0 idle
    ui_motion_until: float | None = None  # monotonic timestamp


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
        except Exception as err:  # transport errors are logged by HA
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
            state.last_hub_pos = pos_i

            # Use hub truth to decide when motion is actually finished
            if state.in_motion and state.pending_target is not None and pos_i is not None:
                if abs(pos_i - state.pending_target) <= 2:
                    _LOGGER.debug(
                        "motion: UID %s hub pos %s ≈ target %s → settled",
                        uid,
                        pos_i,
                        state.pending_target,
                    )
                    state.in_motion = False
                    state.pending_target = None

        return by_uid

    def _pending_uids(self) -> list[str]:
        """Return UIDs that we still consider in-motion for hub tracking."""
        return [
            uid
            for uid, st in self._motion.items()
            if st.in_motion and st.pending_target is not None
        ]

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
                if state.pending_target is not None and abs(pos_i - state.pending_target) <= 2:
                    _LOGGER.debug(
                        "cache prune: UID %s pos %s ≈ target %s → settled",
                        uid,
                        pos_i,
                        state.pending_target,
                    )
                    state.in_motion = False
                    state.pending_target = None

        return not self._pending_uids()

    def get_effective_position(self, uid: str) -> int | None:
        """Return the position we want the UI to show for a shade.

        While in_motion, we keep reporting the pending target; once finished,
        we fall back to the latest hub position and, if needed, the cached status.
        """
        uid = str(uid)
        state = self._motion.get(uid)
        if state and state.in_motion and state.pending_target is not None:
            return state.pending_target
        if state and state.last_hub_pos is not None:
            return state.last_hub_pos

        # Fallback: derive from cached data if we don't have a motion state (e.g. right after restart)
        try:
            raw = (self.data or {}).get("status", {}).get(uid, {}).get("BottomRailPosition")
        except Exception:
            raw = None
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def get_motion_state(self, uid: str) -> ShadeMotionState | None:
        """Expose motion state for entities (e.g. to calculate opening/closing)."""
        return self._motion.get(str(uid))

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

        # Core motion / fake position
        state.in_motion = True
        state.pending_target = target_i
        state.last_control_ts = now
        state.retry_attempted = False
        self._last_global_control_ts = now

        # UI motion timing based on distance to travel
        # Use effective position first, then last_hub_pos, else assume 0 as baseline.
        base_pos = self.get_effective_position(uid_s)
        if base_pos is None:
            base_pos = state.last_hub_pos if state.last_hub_pos is not None else 0
        try:
            base_pos_i = int(base_pos)
        except (TypeError, ValueError):
            base_pos_i = 0

        distance = abs(target_i - base_pos_i)
        duration = distance * UI_SECONDS_PER_PERCENT if distance > 0 else 0.0

        if distance > 0 and duration <= 0:
            duration = UI_SECONDS_PER_PERCENT

        if distance > 0:
            if target_i > base_pos_i:
                state.ui_motion_dir = 1  # opening
            elif target_i < base_pos_i:
                state.ui_motion_dir = -1  # closing
            else:
                state.ui_motion_dir = 0
            state.ui_motion_until = now + duration if state.ui_motion_dir != 0 else None
        else:
            # No travel → no opening/closing UI indication
            state.ui_motion_dir = 0
            state.ui_motion_until = None

        _LOGGER.debug(
            "register_command: UID %s from %s -> %s (distance=%s) ui_dir=%s, ui_for=%.2fs",
            uid_s,
            base_pos_i,
            target_i,
            distance,
            state.ui_motion_dir,
            duration,
        )

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
                    _LOGGER.warning(
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
                    _LOGGER.debug("long-poll: pending cleared per cached status, stopping")
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

                _LOGGER.debug("long-poll: hold %.2fs from ts=%s", timeout, self._notif_ts)

                try:
                    raw = await self.api.notification(timestamp=self._notif_ts, timeout=timeout)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception(
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

            _LOGGER.info("long-poll: all commanded shades settled; stopping watcher")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("long-poll watcher error")
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

        state = self._motion.setdefault(uid_s, ShadeMotionState())
        if state.retry_attempted:
            return

        # Quick check from motion state first
        if state.pending_target is not None and state.last_hub_pos is not None:
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

        if (pos is None or abs(pos - int(target)) > 2) and latest and latest.get("id") == cmd_id:
            _LOGGER.debug(
                "verify_and_retry: UID %s not at target after %.1fs (pos=%s, want=%s) -> retry",
                uid_s,
                delay,
                pos,
                target,
            )
            # Mark as retried and send one more control
            state.retry_attempted = True
            state.last_control_ts = time.monotonic()
            self._last_global_control_ts = state.last_control_ts
            await self.api.control(uid_s, bottom=int(target))
            await self.async_request_refresh()
