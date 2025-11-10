from __future__ import annotations

import logging
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
        # Initial handshake + discovery
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
            # Copy common fields we care about
            for k in ("BottomRailPosition", "BatteryVoltage", "Name"):
                if k in item:
                    cur[k] = item[k]

        return {
            "thing_name": self.api.thing_name,
            "peripherals": self._peripherals,
            "status": by_uid,
        }
