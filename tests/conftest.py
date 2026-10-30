"""Shared fixtures: a fake ShadeAuto hub served through Home Assistant's aiohttp mocker.

The fixture JSON under ``tests/fixtures`` was captured from a real NienMade hub
(firmware 6.1.25, five Norman PerfectSheer shades on motor firmware 2.4.1) and
scrubbed of identifying values. ``FakeHub`` serves those documents and keeps live
per-shade state so tests can issue commands and watch the integration converge.
Other shade families are added at runtime with :meth:`FakeHub.add_shade`; the
files themselves stay real captures.
"""

from __future__ import annotations

import asyncio
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

# Entity ids follow the hub's PeripheralName for each shade (device name + has_entity_name).
SLUG = {
    KITCHEN: "kitchen",
    DINING: "perfectsheer_38a4",
    OFFICE: "office",
    GUEST: "guest_room",
    BEDROOM: "master_bedroom",
}
COVER = {uid: f"cover.{slug}" for uid, slug in SLUG.items()}
BATTERY = {uid: f"sensor.{slug}_battery" for uid, slug in SLUG.items()}
BATTERY_LOW = {uid: f"binary_sensor.{slug}_battery_low" for uid, slug in SLUG.items()}

BOTTOM = "BottomRailPosition"
MIDDLE = "MiddleRailPosition"
MOTOR_STOP = "MotorStop"
TARGET = {BOTTOM: "TargetBottomRailPosition", MIDDLE: "TargetMiddleRailPosition"}

# How the hub moves each family's rails, as FakeHub models them. Kept apart
# from the integration's own profile table on purpose: the tests check that
# table against this one.
SHEER = "sheer"
SINGLE_RAIL = "single_rail"
TWO_RAIL = "two_rail"


def hub_family(module_type: int, module_detail: int) -> str:
    """The rail behaviour of a (ModuleType, ModuleDetail) pair; unknown pairs move like single-rail shades."""
    if (module_type, module_detail) in ((48, 3), (49, 3)):
        return SHEER
    if module_type == 33 or (module_type == 32 and module_detail in (3, 4, 5)):
        return TWO_RAIL
    return SINGLE_RAIL


def load_json(name: str) -> Any:
    """Return the parsed fixture ``tests/fixtures/<name>.json``."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _now() -> int:
    return int(time.time())


class FakeHub:
    """In-memory stand-in for the hub's ``/NM/v1`` endpoints.

    Shade state lives in :attr:`shades` (keyed by int UID) using the hub's own
    field names, and ``/control`` follows the rules observed on the real hub
    (see docs/HUB_PROTOCOL.md), per family (:func:`hub_family`):

    * ``MiddleRailPosition`` without ``BottomRailPosition`` is rejected with ``Error 2``;
    * sheer shades (the fixtures): a command without ``MiddleRailPosition``
      resets the vane target to 0, and vanes are only honoured at bottom 0
      (the target is coerced to 0 otherwise);
    * two-rail shades: both rails are independent heights; a command without
      ``MiddleRailPosition`` sets the middle target to 0, a command with both
      sets both targets, nothing is coerced, and both rails settle;
    * single-rail shades (and unknown pairs): only the bottom rail moves; the
      middle rail always reads 0 and its target stays whatever the test set
      (an app preset can leave it at 100) whatever is commanded;
    * ``MotorStop`` stops the motor; the hub then reports nothing for ~35 s and
      finally sets ``BottomRailPosition`` and ``TargetBottomRailPosition`` to
      the stop point in one update, with a sheer shade's vanes and their target
      at 0 when the stop point is above 0 (log C4b), and a two-rail shade's
      middle target at its middle position;
    * the reply echoes the applied fields plus ``Error 0``.

    Targets change immediately; actual positions jump to the target either at
    once (``settle_after_polls`` is ``None``) or on the N-th ``/status`` call
    after the command, which is how the real hub reports end states only.
    ``landing_offset`` makes a shade land that far from its target (a real
    shade may well report 99 for a target of 100). A stop lands at
    ``stop_point`` (default: wherever the actual position is, i.e. the shade
    had not moved) and is reported at once or, with ``stop_reports_after_polls``,
    on the N-th ``/status`` call after the stop. ``stuck`` keeps actuals where
    they are (a shade that never moves), ``ignore_commands`` answers ``/control``
    with ``Error 0`` but changes nothing, ``offline`` makes every request raise,
    ``reject`` makes ``/control`` answer ``Error 2``, ``omit_fields`` leaves
    fields out of every ``/status`` record (a shade the hub reports less about),
    and ``control_gate``, an :class:`asyncio.Event`, holds every ``/control``
    post after it is recorded in :attr:`commands` and before the hub applies it,
    until the event is set (a slow round trip, so a test can plan a second
    command while the first is in flight).
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
        self.settle_after_polls: int | None = None
        self.landing_offset = 0
        self.stop_point: int | None = None
        self.stop_reports_after_polls: int | None = None
        self.stuck = False
        self.ignore_commands = False  # answer /control with Error 0 but change nothing
        self.reject = False
        self.offline = False
        self.omit_fields: tuple[str, ...] = ()
        self.control_gate: asyncio.Event | None = None
        self.status_calls = 0
        self._polls_until_settle: dict[int, int] = {}
        self._polls_until_stop: dict[int, tuple[int, int]] = {}  # uid -> (polls left, stop point)
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

    def add_shade(
        self,
        uid: int,
        name: str,
        module_type: int,
        module_detail: int,
        *,
        bottom: int = 0,
        middle: int = 0,
        target_middle: int | None = None,
    ) -> None:
        """Add a synthetic shade to ``/GetAllPeripheral`` (in a group of its own) and ``/status``.

        Call it before setting up the integration. Its rails rest at ``bottom``
        and ``middle``; ``target_middle`` sets a middle target that differs
        from the middle position (a single-rail shade's stuck preset target).
        The ``/status`` record copies a fixture shade's other fields.
        """
        room = self.peripheral_doc["results"]["RoomList"][0]
        group_id = max(int(group["GroupID"]) for group in room["GroupList"]) + 1
        room["GroupList"].append(
            {
                "GroupID": str(group_id),
                "GroupName": f"Group_{group_id}",
                "Sorting": str(group_id),
                "PeripheralList": [
                    {
                        "PeripheralUID": str(uid),
                        "PeripheralName": name,
                        "Sorting": "0",
                        "ModuleType": str(module_type),
                        "ModuleDetail": str(module_detail),
                    }
                ],
            }
        )
        template = next(iter(self.shades.values()))
        self.shades[uid] = {
            **template,
            "PeripheralUID": uid,
            "GroupID": group_id,
            "ModuleType": module_type,
            "ModuleDetail": module_detail,
            BOTTOM: bottom,
            TARGET[BOTTOM]: bottom,
            MIDDLE: middle,
            TARGET[MIDDLE]: middle if target_middle is None else target_middle,
        }

    def family(self, uid: int) -> str:
        """Which of the hub's rail rules apply to ``uid`` (see the class docstring)."""
        shade = self.shades[uid]
        return hub_family(int(shade["ModuleType"]), int(shade["ModuleDetail"]))

    def position(self, uid: int, field: str = BOTTOM) -> int:
        """Return the hub's actual position of one axis."""
        return int(self.shades[uid][field])

    def target(self, uid: int, field: str = BOTTOM) -> int:
        """Return the hub's target position of one axis."""
        return int(self.shades[uid][TARGET[field]])

    def external_move(self, uid: int, *, bottom: int | None = None, middle: int | None = None) -> None:
        """Simulate a finished move by the vendor app or a remote (target and actual change together)."""
        for field, value in ((BOTTOM, bottom), (MIDDLE, middle)):
            if value is not None:
                self.shades[uid][TARGET[field]] = value
                self.shades[uid][field] = value

    def external_command(self, uid: int, *, bottom: int, middle: int = 0) -> None:
        """Simulate the vendor app issuing a command: targets change now, actuals settle later."""
        self.shades[uid][TARGET[BOTTOM]] = bottom
        self.shades[uid][TARGET[MIDDLE]] = middle
        self._schedule_settle(uid)

    def _schedule_settle(self, uid: int) -> None:
        if self.stuck:
            return
        if self.settle_after_polls is None:
            self._settle(uid)
        else:
            self._polls_until_settle[uid] = self.settle_after_polls

    def _settle(self, uid: int) -> None:
        """Finish the move: actuals land at the targets (plus ``landing_offset``), as the shade's family allows.

        Sheer vanes only count at bottom 0; a single-rail shade has no middle rail to move.
        """
        shade = self.shades[uid]
        family = self.family(uid)
        if family == SHEER and shade[TARGET[BOTTOM]] != 0:
            shade[TARGET[MIDDLE]] = 0
        for field in (BOTTOM, MIDDLE):
            shade[field] = max(0, min(100, int(shade[TARGET[field]]) + self.landing_offset))
        if family == SINGLE_RAIL:
            shade[MIDDLE] = 0

    def _report_stop(self, uid: int, point: int) -> None:
        """Report where the motor stopped, the way the hub did ~35 s after MotorStop (log C4b)."""
        shade = self.shades[uid]
        shade[BOTTOM] = shade[TARGET[BOTTOM]] = point
        family = self.family(uid)
        if family == TWO_RAIL:
            # Unverified for the middle rail; modelled as a rail that had not moved.
            shade[TARGET[MIDDLE]] = shade[MIDDLE]
        elif family == SINGLE_RAIL:
            return  # no middle rail; its target stays whatever it was
        elif point != 0:
            shade[MIDDLE] = shade[TARGET[MIDDLE]] = 0
        else:
            # What a stop at bottom 0 does to the vane target is unverified; keep the actual.
            shade[TARGET[MIDDLE]] = shade[MIDDLE]

    def advance(self) -> None:
        """Called on every /status: settle shades, and report stops, whose countdown has run out."""
        for uid in list(self._polls_until_settle):
            self._polls_until_settle[uid] -= 1
            if self._polls_until_settle[uid] <= 0:
                del self._polls_until_settle[uid]
                self._settle(uid)
        for uid in list(self._polls_until_stop):
            polls, point = self._polls_until_stop[uid]
            if polls - 1 <= 0:
                del self._polls_until_stop[uid]
                self._report_stop(uid, point)
            else:
                self._polls_until_stop[uid] = (polls - 1, point)

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
            "Peripherals": [{k: v for k, v in s.items() if k not in self.omit_fields} for s in self.shades.values()],
            "RequestTimestamp": now,
            "Timestamp": now * 1000,
        }
        return self._ok(url, body)

    async def _control(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        self._accept("control", data)
        assert data is not None
        self.commands.append(data)
        if self.control_gate is not None:
            await self.control_gate.wait()
        uid = int(data["PeripheralUID"])
        now = _now()
        envelope = {"TaskID": data.get("TaskID"), "RequestTimestamp": now, "Timestamp": now * 1000}
        if self.reject or (MIDDLE in data and BOTTOM not in data):
            return self._ok(url, {"PeripheralUID": uid, "Error": 2, **envelope})

        shade = self.shades[uid]
        applied: dict[str, Any] = {}
        if self.ignore_commands:
            applied = {k: v for k, v in data.items() if k in (BOTTOM, MIDDLE, MOTOR_STOP)}
        elif MOTOR_STOP in data:
            # The motor stops; the hub reports the stop point now or after N polls (see the class docstring).
            self._polls_until_settle.pop(uid, None)
            point = self.position(uid) if self.stop_point is None else self.stop_point
            if self.stop_reports_after_polls is None:
                self._report_stop(uid, point)
            else:
                self._polls_until_stop[uid] = (self.stop_reports_after_polls, point)
            applied[MOTOR_STOP] = data[MOTOR_STOP]
        elif BOTTOM in data:
            self._polls_until_stop.pop(uid, None)  # a new move supersedes an unreported stop
            shade[TARGET[BOTTOM]] = int(data[BOTTOM])
            middle = int(data.get(MIDDLE, 0))  # no MiddleRailPosition -> middle target 0
            if self.family(uid) != SINGLE_RAIL:
                shade[TARGET[MIDDLE]] = middle
            applied[BOTTOM] = shade[TARGET[BOTTOM]]
            applied[MIDDLE] = middle
            self._schedule_settle(uid)
        else:
            applied = {k: v for k, v in data.items() if k not in ("PeripheralUID", "TaskID", "Timestamp", "ThingName")}
        return self._ok(url, {"PeripheralUID": uid, **applied, "Error": 0, **envelope})

    async def _notification(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        """Answer empty at once; the integration does not read this endpoint.

        The real hub keeps the reply open as a never-ending stream of events
        (docs/HUB_PROTOCOL.md 2.5), which only ``tools/hubwatch.py --notify`` reads.
        """
        self._accept("notification", data)
        return self._ok(url, None, text="")


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
