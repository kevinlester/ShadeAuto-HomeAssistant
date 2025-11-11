from __future__ import annotations

import time
import logging
import asyncio
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)


def _now_ts() -> int:
    return int(time.time())


def _find_dicts_with_key(obj: Any, key: str):
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from _find_dicts_with_key(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _find_dicts_with_key(item, key)


class ShadeAutoApi:
    """Thin async client for the ShadeAuto hub local HTTP endpoints."""

    def __init__(self, host: str, session: ClientSession) -> None:
        self._host = host
        self._base = f"http://{host}:10123"
        self._session = session
        self.thing_name: Optional[str] = None
        # Reliability helpers
        self._cmd_lock = asyncio.Lock()
        self._last_send = 0.0

    @property
    def host(self) -> str:
        return self._host

    async def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        url = f"{self._base}{path}"
        timeout = ClientTimeout(total=10)
        _LOGGER.debug("POST %s %s", url, payload)
        async with self._session.post(url, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def registration(self) -> Dict[str, Any]:
        data = await self._post("/NM/v1/registration", {"Timestamp": _now_ts()})
        if isinstance(data, dict):
            self.thing_name = data.get("ThingName") or data.get("thingName") or self.thing_name
        return data

    async def get_all_peripheral(self) -> List[Dict[str, Any]]:
        payload = {"ThingName": self.thing_name, "TaskID": 1, "Timestamp": _now_ts()}
        data = await self._post("/NM/v1/GetAllPeripheral", payload)
        return list(_find_dicts_with_key(data, "PeripheralUID"))

    async def status(self) -> List[Dict[str, Any]]:
        data = await self._post("/NM/v1/status", {"Timestamp": _now_ts()})
        return list(_find_dicts_with_key(data, "PeripheralUID"))

    def _task_id(self) -> int:
        # millisecond-ish unique ID in signed 31-bit range
        return int(time.time() * 1000) & 0x7FFFFFFF

    async def control(self, uid: int | str, *, bottom: int | None = None) -> Dict[str, Any]:
        """Move a shade. Positions are 0..100 (BottomRailPosition only)."""
        payload: Dict[str, Any] = {
            "PeripheralUID": int(uid) if str(uid).isdigit() else uid,
            "TaskID": self._task_id(),
            "Timestamp": _now_ts(),
        }
        if self.thing_name:
            payload["ThingName"] = self.thing_name
        if bottom is not None:
            payload["BottomRailPosition"] = int(bottom)

        # serialize and space commands per hub to avoid drops
        async with self._cmd_lock:
            from .const import SEND_SPACING_SEC  # avoid import cycle
            gap = SEND_SPACING_SEC - (time.time() - self._last_send)
            if gap > 0:
                await asyncio.sleep(gap)
            resp = await self._post("/NM/v1/control", payload)
            self._last_send = time.time()
            return resp
