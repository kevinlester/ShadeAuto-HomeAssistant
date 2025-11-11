from __future__ import annotations

import logging
import asyncio
from datetime import timedelta
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ShadeAutoApi

_LOGGER = logging.getLogger(__name__)


class ShadeAutoCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Polls hub status and caches peripherals + latest state."""

    def __init__(self, hass: HomeAssistant, api: ShadeAutoApi, poll_seconds: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="ShadeAuto Coordinator",
            update_interval=timedelta(seconds=poll_seconds),
        )
        self.api = api
        self._peripherals: Dict[str, Dict[str, Any]] = {}

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
        except Exception as err:
            raise UpdateFailed(f"status error: {err}") from err

        by_uid: Dict[str, Dict[str, Any]] = {}
        for item in status:
            uid = str(item.get("PeripheralUID") or "")
            if not uid:
                continue
            cur = by_uid.setdefault(uid, {})
            for k in ("BottomRailPosition", "BatteryVoltage", "Name"):
                if k in item:
                    cur[k] = item[k]

        return {"thing_name": self.api.thing_name, "peripherals": self._peripherals, "status": by_uid}

    async def async_burst_refresh(self, interval: float, cycles: int) -> None:
        """Poll /status a bit after commands so we don't interleave between controls."""
        delay = max(0.1, float(interval))
        # Defer the very first poll to avoid landing between two control posts
        await asyncio.sleep(delay)
        for _ in range(max(0, cycles)):
            await self.async_request_refresh()
            await asyncio.sleep(delay)

    async def async_verify_and_retry(
        self, uid: str, target: int, *, prev: int | None = None,
        moved_threshold: int = 1, delay: float = 0.50
    ) -> None:
        """Retry once only if the shade hasn't started moving after the command."""
        await asyncio.sleep(max(0.1, delay))
        await self.async_request_refresh()
        st = self.data.get("status", {}).get(str(uid), {})
        pos_raw = st.get("BottomRailPosition")
        try:
            pos = int(pos_raw) if pos_raw is not None else None
        except (TypeError, ValueError):
            pos = None
        # If we have a 'before' reading and the position hasn't changed by >= moved_threshold, resend once.
        if prev is not None and pos is not None and abs(pos - int(prev)) < moved_threshold:
            _LOGGER.debug("verify_and_retry: UID %s did not move (prev=%s, now=%s); retrying once", uid, prev, pos)
            await self.api.control(uid, bottom=int(target))
            await self.async_request_refresh()