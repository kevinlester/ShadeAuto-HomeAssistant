"""Unit tests for the pure parts of tools/hubwatch.py."""

from __future__ import annotations

from datetime import datetime
import threading
from typing import Any
import urllib.error

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


def test_notify_loop_prints_events_and_backs_off_after_errors(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    stop = threading.Event()
    answers: list[Any] = [urllib.error.URLError("hub went away"), '{"Event": 1}', ""]

    def notification(timestamp: int, hold_sec: float) -> str:
        answer = answers.pop(0)
        if not answers:
            stop.set()
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(fake, "notification", notification)
    slept: list[float] = []
    monkeypatch.setattr(hubwatch, "_sleep", slept.append)

    hubwatch._notify_loop(fake, stop)

    assert slept == [2]
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("NOTIFY error: <urlopen error hub went away>")
    assert out[1].endswith('NOTIFY {"Event": 1}')
    assert out[2].endswith("NOTIFY (hub returned without an event)")
