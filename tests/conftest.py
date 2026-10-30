"""Shared fixtures: a fake ShadeAuto hub served through Home Assistant's aiohttp mocker.

The fixture JSON under ``tests/fixtures`` was captured from a real NienMade hub
(firmware 6.1.25, five Norman PerfectSheer shades on motor firmware 2.4.1) and
scrubbed of identifying values. ``FakeHub`` serves those documents and keeps live
per-shade state so tests can issue commands and watch the integration converge.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.shadeauto.const import DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "192.168.3.190"
BASE = f"http://{HOST}:10123/NM/v1"

# Shade UIDs as they appear in the fixtures, with their state in status.json.
KITCHEN = 12177  # fully down (bottom 0), vanes half open (middle 50)
DINING = 14500  # fully up (bottom 100)
OFFICE = 23500  # bottom 35
GUEST = 23388  # bottom 30
BEDROOM = 3190  # fully up (bottom 100)
ALL_UIDS = (KITCHEN, DINING, OFFICE, GUEST, BEDROOM)

BOTTOM = "BottomRailPosition"
MIDDLE = "MiddleRailPosition"
TARGET = {BOTTOM: "TargetBottomRailPosition", MIDDLE: "TargetMiddleRailPosition"}


def load_json(name: str) -> Any:
    """Return the parsed fixture ``tests/fixtures/<name>.json``."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _now() -> int:
    return int(time.time())


class FakeHub:
    """In-memory stand-in for the hub's ``/NM/v1`` endpoints.

    Shade state lives in :attr:`shades` (keyed by int UID) using the hub's own
    field names. ``/control`` records the payload in :attr:`commands` and sets
    the ``Target*`` fields; each ``/status`` call then moves the actual position
    toward the target by :attr:`step` units (``None`` = instantly). Set
    :attr:`offline` to make every request raise a connection error and
    :attr:`reject` to make ``/control`` answer with the hub's error envelope.
    """

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        self.registration: dict[str, Any] = load_json("registration")
        self.peripheral_doc: dict[str, Any] = load_json("get_all_peripheral")
        status = load_json("status")
        self.status_top: dict[str, Any] = {k: v for k, v in status.items() if k != "Peripherals"}
        self.shades: dict[int, dict[str, Any]] = {p["PeripheralUID"]: dict(p) for p in status["Peripherals"]}
        self.thing_name: str = self.registration["ThingName"]

        self.commands: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.step: int | None = None
        self.reject = False
        self.offline = False
        self.status_calls = 0
        self._last_controlled: int | None = None
        self._mocker = mocker
        self.add_host(HOST)

    def add_host(self, host: str) -> None:
        """Answer on another host as well (reconfigure tests move the hub)."""
        base = f"http://{host}:10123/NM/v1"
        self._mocker.post(f"{base}/registration", side_effect=self._registration)
        self._mocker.post(f"{base}/GetAllPeripheral", side_effect=self._get_all_peripheral)
        self._mocker.post(f"{base}/status", side_effect=self._status)
        self._mocker.post(f"{base}/control", side_effect=self._control)
        self._mocker.post(f"{base}/notification", side_effect=self._notification)

    # ----- helpers for tests -----

    def position(self, uid: int, field: str = BOTTOM) -> int:
        """Return the hub's actual position of one axis."""
        return int(self.shades[uid][field])

    def target(self, uid: int, field: str = BOTTOM) -> int:
        """Return the hub's target position of one axis."""
        return int(self.shades[uid][TARGET[field]])

    def external_move(self, uid: int, *, bottom: int | None = None, middle: int | None = None) -> None:
        """Simulate the vendor app or a remote moving a shade (target and actual change together)."""
        for field, value in ((BOTTOM, bottom), (MIDDLE, middle)):
            if value is not None:
                self.shades[uid][TARGET[field]] = value
                self.shades[uid][field] = value

    def advance(self) -> None:
        """Move every actual position one ``step`` toward its target."""
        if self.step is None:
            return
        for shade in self.shades.values():
            for field, target_field in TARGET.items():
                actual, target = int(shade[field]), int(shade[target_field])
                if actual == target:
                    continue
                delta = min(self.step, abs(target - actual))
                shade[field] = actual + delta if target > actual else actual - delta

    # ----- endpoint handlers (signature required by AiohttpClientMocker) -----

    def _accept(self, path: str, data: dict[str, Any] | None) -> None:
        if self.offline:
            raise aiohttp.ClientConnectionError("hub offline")
        self.requests.append((path, data))

    @staticmethod
    def _ok(url: Any, body: Any, *, text: str | None = None) -> AiohttpClientMockResponse:
        if text is not None:
            return AiohttpClientMockResponse("POST", url, text=text)
        return AiohttpClientMockResponse("POST", url, json=body)

    async def _registration(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        self._accept("registration", data)
        return self._ok(url, self.registration)

    async def _get_all_peripheral(
        self, method: str, url: Any, data: dict[str, Any] | None
    ) -> AiohttpClientMockResponse:
        self._accept("GetAllPeripheral", data)
        # The real hub rejects the request unless ThingName matches its own.
        if not data or data.get("ThingName") != self.thing_name:
            return self._ok(url, load_json("error_invalid"))
        return self._ok(url, self.peripheral_doc)

    async def _status(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        self._accept("status", data)
        self.status_calls += 1
        self.advance()
        now = _now()
        body = {
            **self.status_top,
            "Peripherals": [dict(s) for s in self.shades.values()],
            "RequestTimestamp": now,
            "Timestamp": now * 1000,
        }
        return self._ok(url, body)

    async def _control(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        self._accept("control", data)
        assert data is not None
        self.commands.append(data)
        now = _now()
        envelope = {"TaskID": data.get("TaskID"), "RequestTimestamp": now, "Timestamp": now * 1000}
        if self.reject:
            return self._ok(
                url, {"status": {"code": 1, "error": "Invalid JSON format or content."}, "results": envelope}
            )
        shade = self.shades[int(data["PeripheralUID"])]
        for field, target_field in TARGET.items():
            if field in data:
                shade[target_field] = int(data[field])
                if self.step is None:
                    shade[field] = int(data[field])
        self._last_controlled = int(data["PeripheralUID"])
        return self._ok(url, {"status": {"code": 0, "error": ""}, "results": envelope})

    async def _notification(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        """Answer the legacy long-poll immediately.

        The real hub holds the connection open; answering at once with an event
        that names the last controlled shade lets the long-poll watcher re-read
        ``/status`` instead of spinning.
        """
        self._accept("notification", data)
        if self._last_controlled is None:
            return self._ok(url, None, text="")
        event = {"Status": _now(), "PeripheralList": [self._last_controlled]}
        return self._ok(url, None, text=json.dumps(event))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load ``custom_components/shadeauto`` in every test."""
    return


@pytest.fixture
def hub(aioclient_mock: AiohttpClientMocker) -> FakeHub:
    """A fake hub answering on the fixture host."""
    return FakeHub(aioclient_mock)


# Options that keep tests deterministic: no inter-command pacing, no delayed
# verify timers and no slider animation task. Tests that exercise those
# features override the options explicitly.
FAST_OPTIONS: dict[str, Any] = {
    "send_spacing_sec": 0.0,
    "verify_enabled": False,
    "estimation_enabled": False,
}


def make_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Build (but do not add) a config entry for the fixture host."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"ShadeAuto ({HOST})",
        unique_id=f"shadeauto_{HOST}",
        data={"host": HOST},
        options={**FAST_OPTIONS, **(options or {})},
    )


@pytest.fixture
def config_entry(hass: Any) -> MockConfigEntry:
    """A config entry for the fixture host, added to hass but not yet set up."""
    entry = make_entry()
    entry.add_to_hass(hass)
    return entry


async def setup_integration(hass: Any, entry: MockConfigEntry) -> None:
    """Set up ``entry`` and wait for the platforms to finish loading."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def integration(hass: Any, hub: FakeHub, config_entry: MockConfigEntry) -> MockConfigEntry:
    """The integration fully set up against the fake hub."""
    await setup_integration(hass, config_entry)
    return config_entry
