"""Unit tests for the pure parts of tools/hubwatch.py."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import http.client
import json
import threading
from typing import Any

import pytest

from tools import hubwatch

from .conftest import KITCHEN, load_json


def test_parse_kv_accepts_ints_and_rejects_everything_else() -> None:
    assert hubwatch.parse_kv(["MiddleRailPosition=50", "MotorStop=170"]) == {
        "MiddleRailPosition": 50,
        "MotorStop": 170,
    }
    assert hubwatch.parse_kv([]) == {}
    for bad in ("MiddleRailPosition", "=5", "Key=", "Key=abc"):
        with pytest.raises(ValueError):
            hubwatch.parse_kv([bad])


def test_build_control_payload_matches_the_integration() -> None:
    payload = hubwatch.build_control_payload(KITCHEN, {"MiddleRailPosition": 50}, "thing", now=1_700_000_000.5)
    assert payload == {
        "PeripheralUID": KITCHEN,
        "TaskID": int(1_700_000_000.5 * 1000) & 0x7FFFFFFF,
        "Timestamp": 1_700_000_000,
        "ThingName": "thing",
        "MiddleRailPosition": 50,
    }
    assert "ThingName" not in hubwatch.build_control_payload(KITCHEN, {}, None)


def test_snapshot_and_diff_track_only_requested_fields() -> None:
    status = load_json("status")
    before = hubwatch.snapshot(status, ("BottomRailPosition", "MiddleRailPosition"))
    assert set(before) == {14500, 23500, 23388, 12177, 3190}
    assert before[KITCHEN] == {"BottomRailPosition": 0, "MiddleRailPosition": 50}

    only_kitchen = hubwatch.snapshot(status, ("BottomRailPosition",), uids=[KITCHEN])
    assert list(only_kitchen) == [KITCHEN]

    for record in status["Peripherals"]:
        if record["PeripheralUID"] == KITCHEN:
            record["MiddleRailPosition"] = 0
            record["BatteryVoltage"] = 90  # not tracked -> not reported
    after = hubwatch.snapshot(status, ("BottomRailPosition", "MiddleRailPosition"))
    assert hubwatch.diff_snapshots(before, after) == [(KITCHEN, "MiddleRailPosition", 50, 0)]
    assert hubwatch.diff_snapshots(after, after) == []


def test_format_change_is_compact_and_stable() -> None:
    when = datetime(2026, 10, 22, 12, 1, 3, 412_000)
    line = hubwatch.format_change(when, 11.0, KITCHEN, "Kitchen", "MiddleRailPosition", 100, 0)
    assert line == "12:01:03.412 + 11.0s  12177 Kitchen            MiddleRailPosition 100 -> 0"
    first = hubwatch.format_change(when, None, KITCHEN, None, "BottomRailPosition", 0, 40)
    assert first.endswith("12177                    BottomRailPosition 0 -> 40")


class _FakeHub(hubwatch.Hub):
    """Hub whose network calls are replaced by canned answers."""

    def __init__(self) -> None:
        super().__init__("example.invalid")
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, payload: dict, *, timeout: float | None = None) -> Any:
        self.posts.append((path, payload))
        if path == "registration":
            return load_json("registration")
        if path == "GetAllPeripheral":
            return load_json("get_all_peripheral")
        if path == "control":
            # The real reply is flat: the UID, the command fields the hub stored
            # as targets, Error, and the request's TaskID/Timestamp echoed back.
            envelope = ("PeripheralUID", "ThingName", "TaskID", "Timestamp")
            return {
                "PeripheralUID": payload["PeripheralUID"],
                **{key: value for key, value in payload.items() if key not in envelope},
                "Error": 0,
                "TaskID": payload["TaskID"],
                "RequestTimestamp": payload["Timestamp"],
                "Timestamp": payload["Timestamp"] * 1000,
            }
        if path == "status":
            return load_json("status")
        raise AssertionError(path)


def test_main_sends_control_and_can_exit_without_watching(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    monkeypatch.setattr(hubwatch, "Hub", lambda host, port: fake)

    argv = ["--host", "h", "--names", "--control", str(KITCHEN), "BottomRailPosition=0", "MiddleRailPosition=50"]
    rc = hubwatch.main([*argv, "--no-watch"])

    assert rc == 0
    paths = [path for path, _ in fake.posts]
    assert paths == ["registration", "GetAllPeripheral", "control"]
    control = fake.posts[-1][1]
    assert control["PeripheralUID"] == KITCHEN
    assert control["BottomRailPosition"] == 0
    assert control["MiddleRailPosition"] == 50
    assert control["ThingName"] == load_json("registration")["ThingName"]
    out = capsys.readouterr().out
    assert "12177: Kitchen" in out
    assert "control 12177 {'BottomRailPosition': 0, 'MiddleRailPosition': 50} -> " in out
    assert '"MiddleRailPosition": 50, "Error": 0' in out


def test_main_watch_prints_initial_snapshot_then_changes(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    second = load_json("status")
    for record in second["Peripherals"]:
        if record["PeripheralUID"] == KITCHEN:
            record["TargetMiddleRailPosition"] = 0
    polls = [load_json("status"), second]
    monkeypatch.setattr(fake, "status", lambda: polls.pop(0))
    monkeypatch.setattr(hubwatch, "Hub", lambda host, port: fake)

    # A fake clock that only moves when the loop sleeps, so a 1 s watch at a
    # 0.5 s interval polls exactly twice however often the loop reads the clock.
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(hubwatch, "_monotonic", lambda: now)
    monkeypatch.setattr(hubwatch, "_sleep", sleep)

    rc = hubwatch.main(["--host", "h", "--duration", "1", "--interval", "0.5", "--uid", str(KITCHEN)])

    assert rc == 0
    assert polls == []
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[1:3] == [str(KITCHEN), "BottomRailPosition=0"]
    assert any("TargetMiddleRailPosition 50 -> 0" in line for line in out)


# Two real events from docs/hub-characterization-2026-10-23.log: the report-in
# after a StatusRequest and the end of an app-driven tilt.
ACK = b'{"Error": "Success."}'
REPORT_IN = b'{"Status": 1792744669337, "PeripheralList": [12177]}'
MOVE_DONE = b'{"Status": 1792744766350, "PeripheralList": [12177]}'


class _FakeStream:
    """Stands in for an open /notification response: ``read1`` hands out ``chunks`` one per call.

    After the chunks it returns ``b""`` (the hub ended the body), or raises
    ``error`` if one is given. Each read advances ``clock`` by ``read_sec`` so
    tests can check the ages the loop prints.
    """

    def __init__(
        self,
        chunks: Iterable[bytes],
        error: BaseException | None = None,
        clock: list[float] | None = None,
        read_sec: float = 0.0,
    ) -> None:
        self.chunks = list(chunks)
        self.error = error
        self.clock = clock
        self.read_sec = read_sec
        self.reads: list[int] = []
        self.closed = False
        self.status = 200
        self.content_type = "application/octet-stream"

    def read1(self, n: int = -1, /) -> bytes:
        self.reads.append(n)
        if self.clock is not None:
            self.clock[0] += self.read_sec
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            raise self.error
        return b""

    def close(self) -> None:
        self.closed = True


def test_read_stream_splits_unframed_objects_at_any_chunk_boundary() -> None:
    # The ack and a report-in in one read, the next object split across two
    # reads, whitespace between objects, then the hub ends the body.
    chunks = [ACK + REPORT_IN, MOVE_DONE[:17], MOVE_DONE[17:] + b" \r\n", REPORT_IN]
    stream = _FakeStream(chunks)

    items = list(hubwatch.read_stream(stream))

    assert items == [json.loads(ACK), json.loads(REPORT_IN), json.loads(MOVE_DONE), json.loads(REPORT_IN)]
    assert stream.reads == [hubwatch.NOTIFY_READ_SIZE] * 5  # four chunks, then the empty read that ends it


def test_stream_splitter_yields_each_object_when_it_completes() -> None:
    splitter = hubwatch.StreamSplitter()
    assert splitter.feed(REPORT_IN[:10]) == []
    assert splitter.feed(REPORT_IN[10:-1]) == []
    assert splitter.feed(REPORT_IN[-1:] + MOVE_DONE) == [json.loads(REPORT_IN), json.loads(MOVE_DONE)]
    assert splitter.flush() == []

    # A UTF-8 character split across two reads survives (UpdateTime and names can carry them).
    name = '{"PeripheralName": "Küche"}'.encode()
    cut = name.index("ü".encode()) + 1
    assert splitter.feed(name[:cut]) == []
    assert splitter.feed(name[cut:]) == [{"PeripheralName": "Küche"}]


def test_stream_splitter_hands_back_text_that_never_parses() -> None:
    # An error page or other non-JSON reply shows up at the end of the body...
    splitter = hubwatch.StreamSplitter()
    assert splitter.feed(REPORT_IN + b"<html>Not Found</html>") == [json.loads(REPORT_IN)]
    assert splitter.flush() == [hubwatch.Unparsed("<html>Not Found</html>")]

    # ...or, on a stream that stays open, once it outgrows the buffer limit.
    small = hubwatch.StreamSplitter(max_buffer=8)
    assert small.feed(b"garbage") == []
    assert small.feed(b"!!") == [hubwatch.Unparsed("garbage!!")]
    assert small.feed(MOVE_DONE) == [json.loads(MOVE_DONE)]


def test_notify_loop_prints_events_as_they_arrive_and_reconnects(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    stop = threading.Event()
    clock = [0.0]
    streams = [
        _FakeStream([ACK, REPORT_IN[:20], REPORT_IN[20:] + MOVE_DONE], clock=clock, read_sec=1.5),
        _FakeStream([ACK], error=TimeoutError("timed out")),
        _FakeStream([], error=http.client.IncompleteRead(b"")),
    ]
    # What each call to open_notification() does, in order; the last one also asks the loop to stop.
    outcomes: list[Any] = [
        streams[0],
        OSError("no route to host"),
        streams[1],
        streams[2],
        ConnectionRefusedError("hub went away"),
    ]

    def open_notification() -> _FakeStream:
        outcome = outcomes.pop(0)
        if not outcomes:
            stop.set()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(fake, "open_notification", open_notification)
    monkeypatch.setattr(hubwatch, "_monotonic", lambda: clock[0])
    slept: list[float] = []
    monkeypatch.setattr(hubwatch, "_sleep", slept.append)

    hubwatch._notify_loop(fake, stop)

    assert outcomes == []
    assert all(stream.closed for stream in streams)
    # One back-off after each ended stream and after each failed connect.
    assert slept == [hubwatch.NOTIFY_RETRY_SEC] * 5
    lines = [line.split("NOTIFY ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        "+   0.0s  stream open: HTTP 200 application/octet-stream",
        '+   1.5s  {"Error": "Success."}',
        '+   4.5s  {"Status": 1792744669337, "PeripheralList": [12177]}',
        '+   4.5s  {"Status": 1792744766350, "PeripheralList": [12177]}',
        "+   6.0s  hub ended the stream; reopening in 2 s",
        "+   0.0s  connect error: no route to host; retrying in 2 s",
        "+   0.0s  stream open: HTTP 200 application/octet-stream",
        '+   0.0s  {"Error": "Success."}',
        "+   0.0s  nothing received for 300 s; reopening in 2 s",
        "+   0.0s  stream open: HTTP 200 application/octet-stream",
        "+   0.0s  read error: IncompleteRead(0 bytes read); reopening in 2 s",
        "+   0.0s  connect error: hub went away; retrying in 2 s",
    ]


def test_notify_loop_stops_reading_once_asked_to(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    stop = threading.Event()
    stream = _FakeStream([ACK, REPORT_IN])
    original = stream.read1

    def read1(n: int = -1, /) -> bytes:
        chunk = original(n)
        if chunk == REPORT_IN:
            stop.set()
        return chunk

    monkeypatch.setattr(stream, "read1", read1)
    monkeypatch.setattr(fake, "open_notification", lambda: stream)
    monkeypatch.setattr(hubwatch, "_sleep", lambda seconds: pytest.fail("no back-off after a stop"))

    hubwatch._notify_loop(fake, stop)

    assert stream.closed
    assert len(stream.reads) == 2  # nothing read after the stop
    assert capsys.readouterr().out.splitlines()[-1].endswith('{"Status": 1792744669337, "PeripheralList": [12177]}')


def test_open_notification_posts_once_and_waits_long_only_for_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []

    class _Socket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    class _Response:
        status = 200

        def __init__(self) -> None:
            self.closed = False

        def getheader(self, name: str) -> str | None:
            return {"Content-Type": "application/octet-stream"}.get(name)

        def read1(self, n: int = -1, /) -> bytes:
            return ACK

        def close(self) -> None:
            self.closed = True

    class _Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.args = (host, port, timeout)
            self.sock: _Socket | None = None
            self.requests: list[tuple[str, str, Any, dict[str, str]]] = []
            self.response = _Response()
            self.closed = False
            created.append(self)

        def request(self, method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> None:
            self.sock = _Socket()
            self.requests.append((method, url, body, headers or {}))

        def getresponse(self) -> _Response:
            assert self.sock is not None
            assert self.sock.timeouts == []  # the headers still come under the normal timeout
            return self.response

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(hubwatch.http.client, "HTTPConnection", _Connection)
    hub = hubwatch.Hub("hub.invalid", 10123)
    hub.thing_name = "thing"

    stream = hub.open_notification()

    (connection,) = created
    assert connection.args == ("hub.invalid", 10123, hub.timeout)
    (method, url, body, headers) = connection.requests[0]
    assert (method, url) == ("POST", "/NM/v1/notification")
    assert headers == {"Content-Type": "application/json"}
    assert json.loads(body)["ThingName"] == "thing"
    assert connection.sock.timeouts == [hubwatch.NOTIFY_IDLE_SEC]
    assert (stream.status, stream.content_type) == (200, "application/octet-stream")
    assert stream.read1(hubwatch.NOTIFY_READ_SIZE) == ACK
    stream.close()
    assert connection.response.closed and connection.closed


def test_open_notification_closes_the_connection_when_the_request_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class _Connection:
        sock = None

        def __init__(self, host: str, port: int, timeout: float) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionRefusedError("refused")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(hubwatch.http.client, "HTTPConnection", _Connection)

    with pytest.raises(ConnectionRefusedError):
        hubwatch.Hub("hub.invalid").open_notification()
    assert closed == [True]


def test_format_notify_shows_wall_clock_and_stream_age() -> None:
    when = datetime(2026, 10, 23, 4, 37, 49, 337_000)
    assert hubwatch.format_notify(when, 10.66, "x") == "04:37:49.337 NOTIFY +  10.7s  x"
    assert hubwatch.describe_event({"Error": "Success."}) == '{"Error": "Success."}'
    assert hubwatch.describe_event(hubwatch.Unparsed("<html>")) == "unparsed: '<html>'"
