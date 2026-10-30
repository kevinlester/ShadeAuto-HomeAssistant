"""Async client for the ShadeAuto hub's local HTTP API.

Every call is a JSON ``POST`` to ``http://<host>:10123/NM/v1/<endpoint>`` with
no authentication. The hub identifies itself with a ``ThingName`` in the
registration response, which later calls echo back. Positions are 0-100:
``BottomRailPosition`` 0 = fully lowered, 100 = fully raised;
``MiddleRailPosition`` is the vane tilt on sheer shades, 0 = closed,
100 = open. See ``docs/HUB_PROTOCOL.md`` for the observed behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import logging
import time
from typing import Any

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from .const import (
    ATTR_BOTTOM_RAIL,
    ATTR_ERROR,
    ATTR_MIDDLE_RAIL,
    ATTR_MOTOR_STOP,
    ATTR_PERIPHERAL_UID,
    DEFAULT_PORT,
    DEFAULT_SEND_SPACING,
    MOTOR_STOP,
    REQUEST_TIMEOUT_SEC,
)
from .util import to_int

_LOGGER = logging.getLogger(__name__)


class ShadeAutoError(Exception):
    """Base class for errors while talking to the hub."""


class ShadeAutoConnectionError(ShadeAutoError):
    """The hub could not be reached, timed out, or answered with an HTTP error or a non-JSON body."""


class ShadeAutoResponseError(ShadeAutoError):
    """The hub answered a query with its error envelope instead of the requested document.

    ``/GetAllPeripheral`` reports this as ``{"status": {"code": 1, "error": ...}}``
    (for instance when the ``ThingName`` does not match), ``/registration`` as a
    non-zero top-level ``Error``. Treating either as "no shades" would let setup
    finish with an empty integration, so it is raised and setup retries instead.
    """

    def __init__(self, path: str, code: int, message: str = "") -> None:
        super().__init__(f"{path}: hub answered error {code}{f' ({message})' if message else ''}")
        self.code = code


class ShadeAutoCommandError(ShadeAutoError):
    """The hub answered a ``/control`` request with a non-zero ``Error`` code."""

    def __init__(self, code: int, payload: dict[str, Any]) -> None:
        super().__init__(f"hub rejected command with Error {code}: {payload}")
        self.code = code
        self.payload = payload


def _now_ts() -> int:
    return int(time.time())


def clamp_position(value: int) -> int:
    """Clamp to the 0-100 range.

    The hub does not validate ranges (it stored 250 verbatim in testing), so
    clamp before sending.
    """
    return max(0, min(100, int(value)))


def find_dicts_with_key(obj: Any, key: str) -> Iterator[dict[str, Any]]:
    """Yield every dict anywhere inside ``obj`` that has ``key``.

    The hub nests peripheral records at different depths depending on the
    endpoint (``Peripherals`` at the top level of ``/status``, but
    ``RoomList -> GroupList -> PeripheralList`` in ``/GetAllPeripheral``), so
    callers search for the ``PeripheralUID`` key instead of hardcoding paths.
    """
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for value in obj.values():
            yield from find_dicts_with_key(value, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_dicts_with_key(item, key)


class ShadeAutoApi:
    """Thin async client for one hub."""

    def __init__(
        self,
        host: str,
        session: ClientSession,
        *,
        port: int = DEFAULT_PORT,
        send_spacing_sec: float = DEFAULT_SEND_SPACING,
    ) -> None:
        self._host = host
        self._base = f"http://{host}:{port}/NM/v1"
        self._session = session
        self.thing_name: str | None = None
        self.send_spacing_sec = float(send_spacing_sec)
        # /control posts are serialised and spaced; see DEFAULT_SEND_SPACING.
        # Spacing is measured on the monotonic clock so a wall-clock step
        # cannot collapse or stretch the gap.
        self._cmd_lock = asyncio.Lock()
        self._last_send = 0.0
        # Timestamp and TaskID are kept strictly increasing per hub as a
        # precaution against two commands in the same second/millisecond
        # (inherited from upstream; not a verified hub requirement).
        self._last_ts = 0
        self._last_task_id = 0

    @property
    def host(self) -> str:
        return self._host

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base}/{path}"
        _LOGGER.debug("POST %s %s", url, payload)
        try:
            async with self._session.post(url, json=payload, timeout=ClientTimeout(total=REQUEST_TIMEOUT_SEC)) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # ValueError covers a body that is not JSON (json.JSONDecodeError),
            # e.g. a captive portal or a different device answering on the port.
            raise ShadeAutoConnectionError(f"{path}: {err}") from err

    async def registration(self) -> dict[str, Any]:
        """Identify the hub; remembers its ``ThingName`` for later calls."""
        data = await self._post("registration", {"Timestamp": _now_ts()})
        if not isinstance(data, dict):
            return {}
        code = to_int(data.get(ATTR_ERROR))
        if code not in (None, 0):
            raise ShadeAutoResponseError("registration", code)
        self.thing_name = data.get("ThingName") or data.get("thingName") or self.thing_name
        return data

    async def get_all_peripheral(self) -> list[dict[str, Any]]:
        """Return the hub's shade records (names, module types).

        Raises :class:`ShadeAutoResponseError` on the hub's error envelope,
        which is what it answers before :meth:`registration` has supplied the
        ``ThingName``.
        """
        payload = {"ThingName": self.thing_name, "TaskID": 1, "Timestamp": _now_ts()}
        data = await self._post("GetAllPeripheral", payload)
        if isinstance(data, dict) and isinstance(status := data.get("status"), dict):
            code = to_int(status.get("code"))
            if code not in (None, 0):
                raise ShadeAutoResponseError("GetAllPeripheral", code, str(status.get("error") or ""))
        return list(find_dicts_with_key(data, ATTR_PERIPHERAL_UID))

    async def status(self) -> list[dict[str, Any]]:
        """Return the live status record of every shade."""
        data = await self._post("status", {"Timestamp": _now_ts()})
        items = list(find_dicts_with_key(data, ATTR_PERIPHERAL_UID))
        if _LOGGER.isEnabledFor(logging.DEBUG):
            summary = ", ".join(
                f"{d.get(ATTR_PERIPHERAL_UID)}:{d.get(ATTR_BOTTOM_RAIL)}/{d.get(ATTR_MIDDLE_RAIL)}" for d in items
            )
            _LOGGER.debug("status positions (uid:bottom/middle): %s", summary)
        return items

    # --- commands -------------------------------------------------------------

    def _next_task_id(self) -> int:
        """A per-command id: milliseconds masked into the hub's signed 31-bit range, never repeated."""
        task_id = int(time.time() * 1000) & 0x7FFFFFFF
        if task_id <= self._last_task_id:
            task_id = (self._last_task_id + 1) & 0x7FFFFFFF
        self._last_task_id = task_id
        return task_id

    def _next_timestamp(self) -> int:
        """Whole seconds, bumped by one when a command lands in the same second as the last."""
        now = _now_ts()
        self._last_ts = now if now > self._last_ts else self._last_ts + 1
        return self._last_ts

    async def control(self, uid: int | str, *, bottom: int, middle: int | None = None) -> dict[str, Any]:
        """Move a shade and return the hub's reply.

        ``bottom`` is always sent: the hub rejects a command that carries only
        ``MiddleRailPosition`` (``Error 2``), and a command without
        ``MiddleRailPosition`` makes the hub reset the vane target to 0. Pass
        ``middle`` explicitly whenever the vanes should stay where they are.
        Vanes are only honoured by the hub while the shade is fully lowered.
        """
        fields: dict[str, Any] = {ATTR_BOTTOM_RAIL: clamp_position(bottom)}
        if middle is not None:
            fields[ATTR_MIDDLE_RAIL] = clamp_position(middle)
        return await self._send_control(uid, fields)

    async def stop(self, uid: int | str) -> dict[str, Any]:
        """Stop a moving shade. The hub then re-targets the shade to wherever it stopped."""
        return await self._send_control(uid, {ATTR_MOTOR_STOP: MOTOR_STOP})

    async def _send_control(self, uid: int | str, fields: dict[str, Any]) -> dict[str, Any]:
        async with self._cmd_lock:
            # Sleeps the remaining gap, re-checked because a timer may fire a
            # hair early; not a busy wait.
            while (gap := self.send_spacing_sec - (time.monotonic() - self._last_send)) > 0:  # noqa: ASYNC110
                await asyncio.sleep(gap)
            # Built inside the lock, after the wait, so commands queued in the
            # same event-loop tick still get distinct TaskIDs and Timestamps.
            payload: dict[str, Any] = {
                ATTR_PERIPHERAL_UID: int(uid) if str(uid).isdigit() else uid,
                "TaskID": self._next_task_id(),
                "Timestamp": self._next_timestamp(),
            }
            if self.thing_name:
                payload["ThingName"] = self.thing_name
            payload.update(fields)
            try:
                reply = await self._post("control", payload)
            finally:
                self._last_send = time.monotonic()

        if not isinstance(reply, dict):
            return {}
        code = reply.get(ATTR_ERROR)
        if isinstance(code, int) and code != 0:
            raise ShadeAutoCommandError(code, payload)
        return reply
