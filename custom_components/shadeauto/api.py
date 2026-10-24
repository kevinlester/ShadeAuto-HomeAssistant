"""Async client for the ShadeAuto hub's local HTTP API.

Every call is a JSON ``POST`` to ``http://<host>:10123/NM/v1/<endpoint>`` with
no authentication. The hub identifies itself with a ``ThingName`` in the
registration response, which later calls echo back. Positions are 0-100:
``BottomRailPosition`` 0 = fully lowered, 100 = fully raised;
``MiddleRailPosition`` is the vane tilt on sheer shades, 0 = closed,
100 = open. ``/notification`` is different: a stream the hub never ends,
read with :meth:`ShadeAutoApi.notification_stream`. See
``docs/HUB_PROTOCOL.md`` for the observed behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator, Mapping
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
    ATTR_STATUS_REQUEST,
    DEFAULT_PORT,
    DEFAULT_SEND_SPACING,
    MOTOR_STOP,
    NEVER_SENT_FIELDS,
    NOTIFY_MAX_BUFFER,
    REQUEST_TIMEOUT_SEC,
    STATUS_REQUEST,
)
from .util import JsonStreamSplitter, to_int

_LOGGER = logging.getLogger(__name__)

# The /notification stream is legitimately silent for as long as nothing
# changes, so only connecting is timed; the caller decides when to give up on
# a stream (it reopens it every NOTIFY_MAX_DURATION_SEC).
STREAM_TIMEOUT = ClientTimeout(
    total=None, connect=REQUEST_TIMEOUT_SEC, sock_connect=REQUEST_TIMEOUT_SEC, sock_read=None
)


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


def check_scope(uid: int | str | None, fields: Mapping[str, Any]) -> None:
    """Refuse a ``/control`` payload that could reach shades other than ``uid``.

    Without a ``PeripheralUID`` the hub turns ``StatusRequest`` into a
    hub-wide report-in, and a hub-wide report-in closed one user's roller
    shades (kedube/ha-norman issue #5); ``ReportBatteryLevel`` is not confined
    by a ``PeripheralUID`` at all (docs/HUB_PROTOCOL.md 4.6). So a
    ``StatusRequest`` needs a real shade id, and the fields in
    ``NEVER_SENT_FIELDS`` are never sent. Raises ``ValueError``.
    """
    if never := NEVER_SENT_FIELDS & fields.keys():
        raise ValueError(f"refusing to send {sorted(never)}: the hub does not confine it to one shade")
    if ATTR_STATUS_REQUEST in fields and not (str(uid).isdigit() and int(str(uid)) > 0):
        raise ValueError(f"refusing to send StatusRequest for {uid!r}: without a shade id it reaches every shade")


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

    async def notification_stream(self) -> AsyncGenerator[Any]:
        """Yield every JSON object the hub writes on ``/notification``, as it arrives.

        The hub answers at once with ``{"Error": "Success."}`` (a string,
        unlike every other endpoint's ``Error``), keeps the response open and
        then writes ``{"Status": <hub ms>, "PeripheralList": [<uid>, ...]}``
        whenever a shade's target or position changes or a shade reports in,
        with nothing between one object and the next (docs/HUB_PROTOCOL.md
        2.5). The iterator ends when the hub closes the stream; a connection
        that cannot be opened or breaks raises
        :class:`ShadeAutoConnectionError`. Only connecting is timed
        (``STREAM_TIMEOUT``): a quiet stream is normal. Close the iterator
        (``contextlib.aclosing``) to drop the connection.
        """
        url = f"{self._base}/notification"
        # kedube found that the hub ignores the body; this is the one our hub
        # was probed with (docs/hub-characterization-2026-10-23.log).
        payload: dict[str, Any] = {"Timestamp": _now_ts(), "Timeout": 300}
        if self.thing_name:
            payload["ThingName"] = self.thing_name
        _LOGGER.debug("POST %s %s (stream)", url, payload)
        splitter = JsonStreamSplitter(NOTIFY_MAX_BUFFER)
        try:
            async with self._session.post(url, json=payload, timeout=STREAM_TIMEOUT) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_any():
                    for item in splitter.feed(chunk):
                        yield item
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # ValueError: the splitter found no JSON in what the hub sent.
            raise ShadeAutoConnectionError(f"notification: {err}") from err
        if splitter.pending:
            _LOGGER.debug("notification stream ended inside an object: %r", splitter.pending[:200])

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

    async def request_status(self, uid: int | str) -> dict[str, Any]:
        """Ask one shade to report in (``StatusRequest: 0``) and return the hub's reply.

        The shade tells the hub where it really is, without moving; the hub
        announces that on the ``/notification`` stream (2.3 s after the
        request on our hub, within 5 s in kedube/ha-norman's tests) and
        ``/status`` shows the shade's new per-shade ``Timestamp`` and any
        position it had missed. It is a radio transmission like any command,
        so it is paced with them.

        This is the only report request the client can send, and only for
        one shade: without a ``PeripheralUID`` the hub turns ``StatusRequest``
        into a hub-wide report-in, and a hub-wide report-in closed one user's
        roller shades (kedube/ha-norman issue #5; docs/HUB_PROTOCOL.md 4.6).
        ``ReportBatteryLevel`` is never sent, because a ``PeripheralUID`` does
        not confine it. Anything but a positive shade id raises ``ValueError``
        and sends nothing (:func:`check_scope`).
        """
        return await self._send_control(uid, {ATTR_STATUS_REQUEST: STATUS_REQUEST})

    async def _send_control(self, uid: int | str, fields: dict[str, Any]) -> dict[str, Any]:
        check_scope(uid, fields)
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
