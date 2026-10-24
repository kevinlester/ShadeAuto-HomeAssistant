#!/usr/bin/env python3
"""Watch a ShadeAuto hub and print every change it reports.

A development aid for the integration: it polls ``/status`` and prints a
timestamped line whenever a tracked field changes, optionally sends one
``/control`` command first, and optionally holds the hub's ``/notification``
stream open in parallel and prints every event the hub pushes.

Examples::

    python3 tools/hubwatch.py --host 192.168.3.190 --names
    python3 tools/hubwatch.py --host 192.168.3.190 --control 12177 BottomRailPosition=0 MiddleRailPosition=50
    python3 tools/hubwatch.py --host 192.168.3.190 --control 12177 MotorStop=170
    python3 tools/hubwatch.py --host 192.168.3.190 --notify --duration 120

    # ask one shade to report in; add Timestamp to the fields to see it happen
    python3 tools/hubwatch.py --host 192.168.3.190 --notify --control 12177 StatusRequest=0 \
        --fields BottomRailPosition,TargetBottomRailPosition,MiddleRailPosition,TargetMiddleRailPosition,Timestamp

Only the standard library is used so it runs anywhere Python 3.10+ does.
"""

from __future__ import annotations

import argparse
import codecs
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
import http.client
import json
import sys
import threading
import time
from typing import Any, Protocol
import urllib.error
import urllib.request

DEFAULT_PORT = 10123
DEFAULT_FIELDS = (
    "BottomRailPosition",
    "TargetBottomRailPosition",
    "MiddleRailPosition",
    "TargetMiddleRailPosition",
    "BatteryVoltage",
    "CalibrationState",
    "SpeedInformation",
)
TOP_LEVEL_FIELDS = ("Error", "PairingMode", "OTA")

NOTIFY_PATH = "/NM/v1/notification"
NOTIFY_READ_SIZE = 4096  # read1() returns what has arrived so far, up to this many bytes
# The hub writes nothing while nothing changes, so a silent stream is normal.
# One that stays silent this long is reopened anyway: kedube/ha-norman found
# that long-lived streams can stop delivering events and reopens every 300 s.
NOTIFY_IDLE_SEC = 300.0
NOTIFY_RETRY_SEC = 2.0  # back-off before reopening a stream that ended or failed
# The hub's objects are a few dozen bytes. Text this long that still does not
# parse is not JSON (an error page, say); it is printed and dropped instead of
# growing the buffer forever.
NOTIFY_MAX_BUFFER = 64 * 1024

# The loops read the clock and sleep through these aliases so tests can drive
# time by patching them on the module instead of counting time.* calls.
_monotonic = time.monotonic
_sleep = time.sleep

Snapshot = dict[int, dict[str, Any]]


# ----- protocol helpers (pure; unit-tested) ----------------------------------


# Copied from custom_components/shadeauto/api.py on purpose: this tool must not
# import the integration, which pulls in aiohttp and homeassistant.
def find_dicts_with_key(obj: Any, key: str) -> Iterator[dict[str, Any]]:
    """Yield every dict nested anywhere in ``obj`` that has ``key``."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for value in obj.values():
            yield from find_dicts_with_key(value, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_dicts_with_key(item, key)


def parse_kv(items: Iterable[str]) -> dict[str, int]:
    """Turn ``["MiddleRailPosition=50", "MotorStop=170"]`` into a dict of ints."""
    fields: dict[str, int] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key or not value.lstrip("-").isdigit():
            raise ValueError(f"expected KEY=INT, got {item!r}")
        fields[key] = int(value)
    return fields


def build_control_payload(
    uid: int, fields: dict[str, int], thing_name: str | None, *, now: float | None = None
) -> dict:
    """The body the integration sends to ``/control``, with ``fields`` merged in."""
    now = time.time() if now is None else now
    payload: dict[str, Any] = {
        "PeripheralUID": int(uid),
        "TaskID": int(now * 1000) & 0x7FFFFFFF,
        "Timestamp": int(now),
    }
    if thing_name:
        payload["ThingName"] = thing_name
    payload.update(fields)
    return payload


def snapshot(status: Any, fields: Iterable[str], uids: Iterable[int] | None = None) -> Snapshot:
    """Reduce a ``/status`` document to ``{uid: {field: value}}`` for the tracked fields."""
    wanted = set(uids) if uids else None
    result: Snapshot = {}
    for record in find_dicts_with_key(status, "PeripheralUID"):
        uid = int(record["PeripheralUID"])
        if wanted is not None and uid not in wanted:
            continue
        result[uid] = {field: record.get(field) for field in fields}
    return result


def diff_snapshots(before: Snapshot, after: Snapshot) -> list[tuple[int, str, Any, Any]]:
    """List ``(uid, field, old, new)`` for every value that changed between two snapshots."""
    changes = []
    for uid, fields in after.items():
        previous = before.get(uid, {})
        for field, value in fields.items():
            if field not in previous:
                continue
            if previous[field] != value:
                changes.append((uid, field, previous[field], value))
    return changes


def format_change(
    when: datetime, since_last: float | None, uid: int, name: str | None, field: str, old: Any, new: Any
) -> str:
    """One printed line per change, e.g. ``12:01:03.412 +11.0s 12177 Kitchen MiddleRailPosition 100 -> 0``."""
    delta = f"+{since_last:5.1f}s" if since_last is not None else "       "
    label = f"{uid} {name}" if name else str(uid)
    return f"{when:%H:%M:%S}.{when.microsecond // 1000:03d} {delta}  {label:<24} {field} {old} -> {new}"


def format_notify(when: datetime, age: float, text: str) -> str:
    """One printed line per stream event; ``age`` is seconds since the stream was opened."""
    return f"{when:%H:%M:%S}.{when.microsecond // 1000:03d} NOTIFY +{age:6.1f}s  {text}"


@dataclass(frozen=True)
class Unparsed:
    """Stream text that never became a JSON object, kept so it can be shown rather than lost."""

    text: str


class StreamSplitter:
    """Cut the hub's ``/notification`` body into whole JSON objects.

    The hub writes bare objects back to back, with no newline or length
    between them, and the socket hands them over in arbitrary pieces: one read
    can end inside an object or hold several. Bytes are decoded incrementally
    (a UTF-8 character may also be split) and ``raw_decode`` takes one object
    at a time off the front of the buffer; an incomplete object stays there
    until the rest arrives.
    """

    def __init__(self, max_buffer: int = NOTIFY_MAX_BUFFER) -> None:
        self._text = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._decoder = json.JSONDecoder()
        self._buffer = ""
        self._max_buffer = max_buffer

    def feed(self, chunk: bytes) -> list[Any]:
        """Add ``chunk`` and return every object it completed, oldest first."""
        self._buffer += self._text.decode(chunk)
        items: list[Any] = []
        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            try:
                item, end = self._decoder.raw_decode(self._buffer)
            except json.JSONDecodeError:
                if len(self._buffer) > self._max_buffer:
                    items.append(Unparsed(self._buffer))
                    self._buffer = ""
                break
            items.append(item)
            self._buffer = self._buffer[end:]
        return items

    def flush(self) -> list[Any]:
        """At the end of the stream: whatever is left, which can only be unparseable."""
        rest = (self._buffer + self._text.decode(b"", final=True)).strip()
        self._buffer = ""
        return [Unparsed(rest)] if rest else []


class Readable(Protocol):
    """What :func:`read_stream` needs from a response: ``http.client.HTTPResponse.read1``."""

    def read1(self, n: int = -1, /) -> bytes: ...


def read_stream(response: Readable, size: int = NOTIFY_READ_SIZE) -> Iterator[Any]:
    """Yield each object from a ``/notification`` body as soon as it is complete, until EOF.

    ``read1`` returns as soon as any bytes have arrived instead of waiting for
    ``size`` of them or for the end of the body; the hub never ends the body on
    its own, so a reader that waits for the end never returns.
    """
    splitter = StreamSplitter()
    while chunk := response.read1(size):
        yield from splitter.feed(chunk)
    yield from splitter.flush()


def describe_event(item: Any) -> str:
    """The printed form of one stream item: compact JSON, or the raw text if it did not parse."""
    if isinstance(item, Unparsed):
        return f"unparsed: {item.text[:800]!r}"
    return json.dumps(item)


# ----- hub client --------------------------------------------------------------


class Hub:
    """Minimal blocking client for the hub's ``/NM/v1`` endpoints."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.base = f"http://{host}:{port}/NM/v1"
        self.timeout = timeout
        self.thing_name: str | None = None

    def post(self, path: str, payload: dict, *, timeout: float | None = None) -> Any:
        request = urllib.request.Request(
            f"{self.base}/{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # The hub only speaks plain http on the LAN; there is no https variant.
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:  # nosec B310
            body = response.read().decode()
        try:
            return json.loads(body)
        except ValueError:
            return body

    def register(self) -> str | None:
        data = self.post("registration", {"Timestamp": int(time.time())})
        if isinstance(data, dict):
            self.thing_name = data.get("ThingName")
        return self.thing_name

    def names(self) -> dict[int, str]:
        """``{uid: PeripheralName}`` from ``/GetAllPeripheral`` (needs :meth:`register` first)."""
        data = self.post("GetAllPeripheral", {"ThingName": self.thing_name, "TaskID": 1, "Timestamp": int(time.time())})
        return {
            int(record["PeripheralUID"]): str(record.get("PeripheralName") or "")
            for record in find_dicts_with_key(data, "PeripheralUID")
        }

    def status(self) -> Any:
        return self.post("status", {"Timestamp": int(time.time())})

    def control(self, uid: int, fields: dict[str, int]) -> Any:
        return self.post("control", build_control_payload(uid, fields, self.thing_name))

    def open_notification(self) -> NotificationStream:
        """Open the ``/notification`` stream; the caller reads it with ``read1`` and closes it.

        The hub sends its headers and ``{"Error": "Success."}`` at once, so
        connecting and the headers get the normal timeout. After that it writes
        only when something changes, so reads of the body wait up to
        ``NOTIFY_IDLE_SEC``.
        """
        # The body the 2026-10-23 probe sent. kedube/ha-norman found that the
        # hub ignores it (an empty body works too), so Timeout is not a hold time.
        payload = {"ThingName": self.thing_name, "Timestamp": int(time.time()), "Timeout": NOTIFY_IDLE_SEC}
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(
                "POST", NOTIFY_PATH, body=json.dumps(payload), headers={"Content-Type": "application/json"}
            )
            sock = connection.sock
            response = connection.getresponse()
            if sock is not None:
                sock.settimeout(NOTIFY_IDLE_SEC)
        except BaseException:
            connection.close()
            raise
        return NotificationStream(connection, response)


class NotificationStream:
    """An open ``/notification`` response together with the connection it arrived on.

    Closing only the response would leave the socket open for as long as the
    connection object lives, so :meth:`close` closes both.
    """

    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self._connection = connection
        self._response = response
        self.status = response.status
        self.content_type = response.getheader("Content-Type")

    def read1(self, n: int = -1, /) -> bytes:
        return self._response.read1(n)

    def close(self) -> None:
        self._response.close()
        self._connection.close()


# ----- watch loop --------------------------------------------------------------


def _print(line: str) -> None:
    print(line, flush=True)


def _notify_loop(hub: Hub, stop: threading.Event) -> None:
    """Hold the ``/notification`` stream open and print every object as it arrives.

    The hub answers ``{"Error": "Success."}`` at once, keeps the response open
    and writes ``{"Status": <hub ms>, "PeripheralList": [<uid>, ...]}`` each time
    a shade's target or position changes. It never ends the response, which is
    why the 0.4.0 version of this loop, which read each response to the end
    behind a 60 s socket timeout, printed no events (docs/HUB_PROTOCOL.md, 2.5).
    A stream that ends, fails or says nothing for ``NOTIFY_IDLE_SEC`` is
    reopened after ``NOTIFY_RETRY_SEC``. The hub does not replay what happened
    in between, so a gap in the stream is a gap in what this prints.
    """
    while not stop.is_set():
        try:
            stream = hub.open_notification()
        except (OSError, http.client.HTTPException) as err:
            _print(format_notify(datetime.now(), 0.0, f"connect error: {err}; retrying in {NOTIFY_RETRY_SEC:g} s"))
            _sleep(NOTIFY_RETRY_SEC)
            continue
        opened = _monotonic()
        _print(format_notify(datetime.now(), 0.0, f"stream open: HTTP {stream.status} {stream.content_type}"))
        try:
            for item in read_stream(stream):
                _print(format_notify(datetime.now(), _monotonic() - opened, describe_event(item)))
                if stop.is_set():
                    return
            reason = "hub ended the stream"
        except TimeoutError:
            reason = f"nothing received for {NOTIFY_IDLE_SEC:g} s"
        except (OSError, http.client.HTTPException) as err:
            reason = f"read error: {err!r}"
        finally:
            stream.close()
        _print(format_notify(datetime.now(), _monotonic() - opened, f"{reason}; reopening in {NOTIFY_RETRY_SEC:g} s"))
        _sleep(NOTIFY_RETRY_SEC)


def watch(hub: Hub, args: argparse.Namespace, names: dict[int, str]) -> None:
    fields = tuple(args.fields.split(",")) if args.fields else DEFAULT_FIELDS
    deadline = _monotonic() + args.duration if args.duration else None
    previous: Snapshot | None = None
    previous_top: dict[str, Any] | None = None
    last_change: float | None = None

    while deadline is None or _monotonic() < deadline:
        try:
            status = hub.status()
        except (urllib.error.URLError, OSError, ValueError) as err:
            _print(f"{datetime.now():%H:%M:%S} status error: {err}")
            _sleep(args.interval)
            continue
        now = datetime.now()
        if args.raw:
            _print(json.dumps(status, separators=(",", ":")))

        current = snapshot(status, fields, args.uid)
        top = {key: status.get(key) for key in TOP_LEVEL_FIELDS} if isinstance(status, dict) else {}
        if previous is None:
            for uid, values in sorted(current.items()):
                label = f"{uid} {names.get(uid, '')}".strip()
                _print(f"{now:%H:%M:%S} {label:<24} " + "  ".join(f"{k}={v}" for k, v in values.items()))
            _print(f"{now:%H:%M:%S} hub " + "  ".join(f"{k}={v}" for k, v in top.items()))
        else:
            for uid, field, old, new in diff_snapshots(previous, current):
                since = _monotonic() - last_change if last_change is not None else None
                _print(format_change(now, since, uid, names.get(uid), field, old, new))
                last_change = _monotonic()
            if previous_top is not None and top != previous_top:
                _print(f"{now:%H:%M:%S} hub {previous_top} -> {top}")
        previous, previous_top = current, top
        _sleep(args.interval)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="hub IP or host name")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between /status polls")
    parser.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = until Ctrl-C)")
    parser.add_argument("--uid", type=int, action="append", help="only show these shade UIDs (repeatable)")
    parser.add_argument("--fields", help="comma-separated field names to track instead of the defaults")
    parser.add_argument("--names", action="store_true", help="look up shade names from the hub")
    parser.add_argument("--raw", action="store_true", help="also dump every /status document as JSON")
    parser.add_argument(
        "--notify", action="store_true", help="hold the /notification stream open in parallel and print its events"
    )
    parser.add_argument(
        "--control",
        nargs="+",
        metavar=("UID", "KEY=VAL"),
        help="send one /control command (e.g. 12177 BottomRailPosition=0 MiddleRailPosition=50) before watching",
    )
    parser.add_argument("--no-watch", action="store_true", help="exit after --control instead of watching")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    hub = Hub(args.host, args.port)
    names: dict[int, str] = {}

    if args.names or args.control or args.notify:
        thing_name = hub.register()
        _print(f"registered with hub; ThingName={thing_name}")
    if args.names:
        names = hub.names()
        for uid, name in sorted(names.items()):
            _print(f"  {uid}: {name}")

    if args.control:
        uid = int(args.control[0])
        fields = parse_kv(args.control[1:])
        if not fields:
            _print("--control needs at least one KEY=VAL")
            return 2
        response = hub.control(uid, fields)
        _print(f"control {uid} {fields} -> {json.dumps(response)}")
        if args.no_watch:
            return 0

    stop = threading.Event()
    if args.notify:
        threading.Thread(target=_notify_loop, args=(hub, stop), daemon=True).start()
    try:
        watch(hub, args, names)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
