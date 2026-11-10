"""Shade profiles: which entities and commands each (ModuleType, ModuleDetail) family gets.

The fixture shades are all PerfectSheer (48/3); every other family is added
to the fake hub at runtime with ``FakeHub.add_shade``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_CLOSE_COVER_TILT,
    SERVICE_OPEN_COVER,
    SERVICE_OPEN_COVER_TILT,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
    SERVICE_STOP_COVER,
    SERVICE_STOP_COVER_TILT,
    CoverState,
)
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.shadeauto.const import (
    AXIS_BOTTOM,
    AXIS_MIDDLE,
    DEFAULT_MOTION_POLL,
    DEFAULT_POLL,
    MIN_VERIFY_DELAY,
    MOTION_FAILSAFE_SEC,
    PROFILE_DRAPE,
    PROFILE_LEGACY,
    PROFILE_SHEER,
    PROFILE_SINGLE_RAIL,
    PROFILE_TWO_RAIL,
    STATUS_REPORT_WAIT_SEC,
    UNTRACKED_SETTLE_SEC,
)
from custom_components.shadeauto.coordinator import ShadeAutoCoordinator, shade_profile
from custom_components.shadeauto.cover import (
    drape_tilt_to_ha,
    drape_tilt_to_hub,
    drape_vanes_for_draw,
    plan_rail_move,
)

from .conftest import (
    ALL_UIDS,
    BOTTOM,
    COVER,
    DRAPE,
    KITCHEN,
    MIDDLE,
    MOTOR_STOP,
    SHEER,
    SINGLE_RAIL,
    STATUS_REQUEST,
    TARGET,
    TWO_RAIL,
    FakeHub,
    hub_family,
    is_status_request,
    make_entry,
    setup_integration,
)

ALL_FEATURES = 255  # lift and tilt
LIFT_ONLY_FEATURES = 15  # OPEN|CLOSE|SET_POSITION|STOP

IDLE = timedelta(seconds=DEFAULT_POLL)
MOTION = timedelta(seconds=DEFAULT_MOTION_POLL)
MOTION_TICK = DEFAULT_MOTION_POLL + 1
VERIFY_OPTIONS = {"verify_enabled": True, "verify_delay_sec": MIN_VERIFY_DELAY}
VERIFY_TICK = MIN_VERIFY_DELAY + 1
REPORT_TICK = STATUS_REPORT_WAIT_SEC + 1  # verify's wait for the shade to report in, which runs out without the stream

# Synthetic shades. Den is a roller whose middle target an app preset left at 100.
DEN = 40001
DEN_COVER = "cover.den"
PATIO = 50001  # two-rail, resting with the bottom rail at 20 and the top rail at 60
PATIO_COVER = "cover.patio"
PATIO_TOP = "cover.patio_top"
LOUNGE = 80001  # a SmartDrape, drawn to 30 with its vanes open (50)
LOUNGE_COVER = "cover.lounge"


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: int) -> None:
    await hass.services.async_call(COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = make_entry(options)
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    return entry


def _coordinator(entry: MockConfigEntry) -> ShadeAutoCoordinator:
    return entry.runtime_data.coordinator


def _commands_for(hub: FakeHub, uid: int) -> list[dict[str, Any]]:
    return [cmd for cmd in hub.commands if cmd["PeripheralUID"] == uid]


def _rails(cmd: dict[str, Any]) -> tuple[int, int]:
    return cmd[BOTTOM], cmd[MIDDLE]


def _add_patio(hub: FakeHub) -> None:
    hub.add_shade(PATIO, "Patio", 33, 3, bottom=20, middle=60)


def _add_lounge(hub: FakeHub) -> None:
    hub.add_shade(LOUNGE, "Lounge", 80, 1, bottom=30, middle=50)


# --- the profile table ------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_type", "module_detail", "profile"),
    [
        # Sheer: vanes on the middle rail.
        (48, 3, PROFILE_SHEER),
        (49, 3, PROFILE_SHEER),
        # Single rail: cellular, roller, roman and the 49 generation, plus any other detail of those types.
        (32, 0, PROFILE_SINGLE_RAIL),
        (32, 1, PROFILE_SINGLE_RAIL),
        (32, 2, PROFILE_SINGLE_RAIL),
        (32, 6, PROFILE_SINGLE_RAIL),
        (48, 1, PROFILE_SINGLE_RAIL),
        (48, 2, PROFILE_SINGLE_RAIL),
        (48, 0, PROFILE_SINGLE_RAIL),
        (49, 1, PROFILE_SINGLE_RAIL),
        (49, 2, PROFILE_SINGLE_RAIL),
        # Two rails: all of type 33 and cellular details 3-5.
        (33, 0, PROFILE_TWO_RAIL),
        (33, 3, PROFILE_TWO_RAIL),
        (33, 4, PROFILE_TWO_RAIL),
        (33, 5, PROFILE_TWO_RAIL),
        (33, 9, PROFILE_TWO_RAIL),
        (32, 3, PROFILE_TWO_RAIL),
        (32, 4, PROFILE_TWO_RAIL),
        (32, 5, PROFILE_TWO_RAIL),
        # SmartDrape: the draw plus vanes on the middle rail, whatever the detail.
        (80, 1, PROFILE_DRAPE),
        (80, 0, PROFILE_DRAPE),
        (80, None, PROFILE_DRAPE),
        # Known families without verified behaviour, and pairs nobody has reported.
        (1, 0, PROFILE_LEGACY),
        (99, 7, PROFILE_LEGACY),
        (None, None, PROFILE_LEGACY),
        (None, 3, PROFILE_LEGACY),
        # Without a detail, a type whose profile depends on it cannot be placed; 33 can.
        (48, None, PROFILE_LEGACY),
        (32, None, PROFILE_LEGACY),
        (33, None, PROFILE_TWO_RAIL),
    ],
)
def test_profile_table(module_type: int | None, module_detail: int | None, profile: str) -> None:
    assert shade_profile(module_type, module_detail) == profile
    if module_type is not None and module_detail is not None:
        # The fake hub's model of each family agrees with the integration's table.
        expected_family = {PROFILE_SHEER: SHEER, PROFILE_TWO_RAIL: TWO_RAIL, PROFILE_DRAPE: DRAPE}.get(
            profile, SINGLE_RAIL
        )
        assert hub_family(module_type, module_detail) == expected_family


async def test_the_fixture_shades_keep_the_sheer_cover(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """The real hub's five PerfectSheer shades: one cover each, lift and tilt, unique ids unchanged."""
    coordinator = _coordinator(integration)
    registry = er.async_get(hass)
    for uid in ALL_UIDS:
        assert coordinator.peripherals[str(uid)].profile == PROFILE_SHEER
        assert hass.states.get(COVER[uid]).attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
        assert registry.async_get(COVER[uid]).unique_id == f"shadeauto_{hub.thing_name}_{uid}"
    assert len(er.async_entries_for_config_entry(registry, integration.entry_id)) == 3 * len(ALL_UIDS)


async def test_module_pair_missing_from_the_peripheral_list_is_taken_from_status(
    hass: HomeAssistant, hub: FakeHub
) -> None:
    hub.add_shade(DEN, "Den", 48, 1, bottom=100)
    listed = hub.peripheral_doc["results"]["RoomList"][0]["GroupList"][-1]["PeripheralList"][0]
    del listed["ModuleType"], listed["ModuleDetail"]
    entry = await _setup(hass)

    peripheral = _coordinator(entry).peripherals[str(DEN)]
    assert (peripheral.module_type, peripheral.module_detail) == (48, 1)
    assert peripheral.profile == PROFILE_SINGLE_RAIL
    assert hass.states.get(DEN_COVER).attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(DEN_COVER).device_id)
    assert device.model == "Shade (type 48/1)"


# --- single rail and legacy ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_type", "module_detail"),
    [(48, 1), (48, 2), (49, 1), (32, 0), (32, 2), (1, 0), (99, 7)],
)
async def test_single_rail_and_legacy_shades_are_lift_only_with_bottom_only_commands(
    hass: HomeAssistant, hub: FakeHub, module_type: int, module_detail: int
) -> None:
    """Exactly what 0.3.0 sent: BottomRailPosition alone, no tilt anywhere."""
    hub.add_shade(DEN, "Den", module_type, module_detail, bottom=100, target_middle=100)
    await _setup(hass)

    state = hass.states.get(DEN_COVER)
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes
    assert state.attributes[ATTR_DEVICE_CLASS] == "shade"
    assert state.attributes["friendly_name"] == "Den"

    await _call(hass, SERVICE_SET_COVER_POSITION, DEN_COVER, **{ATTR_POSITION: 30})
    await _call(hass, SERVICE_CLOSE_COVER, DEN_COVER)
    await _call(hass, SERVICE_OPEN_COVER, DEN_COVER)
    commands = _commands_for(hub, DEN)
    assert [cmd[BOTTOM] for cmd in commands] == [30, 0, 100]
    assert all(MIDDLE not in cmd for cmd in commands)
    assert hass.states.get(DEN_COVER).attributes[ATTR_CURRENT_POSITION] == 100


@pytest.mark.parametrize(
    ("module_type", "module_detail"),
    [(48, 1), (1, 0), (99, 7)],  # a roller, a shutter and an unknown pair: single_rail and legacy alike
)
async def test_a_stuck_middle_target_neither_keeps_a_single_rail_shade_moving_nor_triggers_a_retry(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, module_type: int, module_detail: int
) -> None:
    """An app preset left TargetMiddleRailPosition at 100 on a shade whose middle rail always reads 0."""
    hub.add_shade(DEN, "Den", module_type, module_detail, bottom=100, target_middle=100)
    entry = await _setup(hass, VERIFY_OPTIONS)
    coordinator = _coordinator(entry)
    assert hub.target(DEN, MIDDLE) == 100
    assert hub.position(DEN, MIDDLE) == 0

    assert not coordinator.is_shade_moving(str(DEN))
    assert coordinator.update_interval == IDLE
    assert hass.states.get(DEN_COVER).state == CoverState.OPEN

    await _call(hass, SERVICE_SET_COVER_POSITION, DEN_COVER, **{ATTR_POSITION: 40})
    assert hub.position(DEN) == 40
    assert hub.target(DEN, MIDDLE) == 100  # still stuck
    assert not coordinator.is_shade_moving(str(DEN))
    assert not coordinator.is_moving(str(DEN), AXIS_MIDDLE)
    assert coordinator.update_interval == IDLE
    state = hass.states.get(DEN_COVER)
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 40

    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, VERIFY_TICK)
    assert len(_commands_for(hub, DEN)) == 1  # the bottom rail arrived, so no retry


async def test_single_rail_retry_is_bottom_only_too(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    hub.add_shade(DEN, "Den", 48, 1, bottom=100, target_middle=100)
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_CLOSE_COVER, DEN_COVER)
    assert hass.states.get(DEN_COVER).state == CoverState.CLOSING
    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, REPORT_TICK)

    commands = _commands_for(hub, DEN)
    assert [is_status_request(cmd) for cmd in commands] == [False, True, False]
    assert commands[1][STATUS_REQUEST] == 0
    assert commands[2][BOTTOM] == 0
    assert MIDDLE not in commands[2]


async def test_unrecognised_module_pair_is_warned_about_once(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """One warning per unknown pair, naming its shades; known-but-unsupported families only log at debug."""
    hub.add_shade(60001, "Mystery One", 99, 7)
    hub.add_shade(60002, "Mystery Two", 99, 7)
    hub.add_shade(60004, "Louvres", 1, 0)
    caplog.set_level(logging.DEBUG, logger="custom_components.shadeauto")
    entry = await _setup(hass)
    await _poll(hass, freezer, DEFAULT_POLL + 1)
    await _poll(hass, freezer, DEFAULT_POLL + 1)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name.startswith("custom_components")]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "ModuleType 99 / ModuleDetail 7" in message
    assert "Mystery One" in message
    assert "Mystery Two" in message
    debug = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert "Louvres (Shutter, ModuleType 1 / ModuleDetail 0)" in debug

    coordinator = _coordinator(entry)
    for uid in (60001, 60002, 60004):
        assert coordinator.peripherals[str(uid)].profile == PROFILE_LEGACY


# --- two rails --------------------------------------------------------------------


async def test_two_rail_shade_gets_a_bottom_cover_and_a_top_cover(hass: HomeAssistant, hub: FakeHub) -> None:
    _add_patio(hub)
    entry = await _setup(hass)
    registry = er.async_get(hass)

    main = registry.async_get(PATIO_COVER)
    top = registry.async_get(PATIO_TOP)
    assert main.unique_id == f"shadeauto_{hub.thing_name}_{PATIO}"
    assert top.unique_id == f"shadeauto_{hub.thing_name}_{PATIO}_middle"
    assert top.translation_key == "top"
    assert main.device_id == top.device_id
    assert dr.async_get(hass).async_get(main.device_id).name == "Patio"

    main_state = hass.states.get(PATIO_COVER)
    top_state = hass.states.get(PATIO_TOP)
    assert main_state.attributes["friendly_name"] == "Patio"
    assert top_state.attributes["friendly_name"] == "Patio Top"
    for state in (main_state, top_state):
        assert state.attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
        assert ATTR_CURRENT_TILT_POSITION not in state.attributes
        assert state.attributes[ATTR_DEVICE_CLASS] == "shade"
        assert state.state == CoverState.OPEN
    assert main_state.attributes[ATTR_CURRENT_POSITION] == 20
    assert top_state.attributes[ATTR_CURRENT_POSITION] == 60
    # One battery sensor and one low-battery sensor per shade, not per rail.
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 3 * len(ALL_UIDS) + 4


async def test_every_two_rail_command_carries_both_rails(hass: HomeAssistant, hub: FakeHub) -> None:
    """A bottom-only command would drag the top rail to 0, so even a lift of the bottom rail restates the top."""
    _add_patio(hub)
    await _setup(hass)

    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_COVER, **{ATTR_POSITION: 40})
    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_TOP, **{ATTR_POSITION: 80})
    await _call(hass, SERVICE_CLOSE_COVER, PATIO_COVER)
    await _call(hass, SERVICE_OPEN_COVER, PATIO_TOP)
    await _call(hass, SERVICE_CLOSE_COVER, PATIO_TOP)
    await _call(hass, SERVICE_OPEN_COVER, PATIO_COVER)

    assert [_rails(cmd) for cmd in _commands_for(hub, PATIO)] == [
        (40, 60),
        (40, 80),
        (0, 80),
        (0, 100),
        (0, 0),
        (100, 100),  # the guard drags the top rail up with the bottom one
    ]
    assert (hub.position(PATIO), hub.position(PATIO, MIDDLE)) == (100, 100)
    assert hass.states.get(PATIO_COVER).attributes[ATTR_CURRENT_POSITION] == 100
    assert hass.states.get(PATIO_TOP).attributes[ATTR_CURRENT_POSITION] == 100


@pytest.mark.parametrize(
    ("options", "entity_id", "service", "position", "expected"),
    [
        # Guard on, no gap. Patio rests at bottom 20 / top 60.
        ({}, PATIO_COVER, SERVICE_OPEN_COVER, None, (100, 100)),
        ({}, PATIO_COVER, SERVICE_CLOSE_COVER, None, (0, 60)),
        ({}, PATIO_COVER, SERVICE_SET_COVER_POSITION, 50, (50, 60)),
        ({}, PATIO_COVER, SERVICE_SET_COVER_POSITION, 80, (80, 80)),
        ({}, PATIO_TOP, SERVICE_OPEN_COVER, None, (20, 100)),
        ({}, PATIO_TOP, SERVICE_CLOSE_COVER, None, (0, 0)),
        ({}, PATIO_TOP, SERVICE_SET_COVER_POSITION, 40, (20, 40)),
        ({}, PATIO_TOP, SERVICE_SET_COVER_POSITION, 10, (10, 10)),
        # Guard on with a 10 % gap: the moved rail stops 10 short of the far end.
        ({"rail_min_gap": 10}, PATIO_COVER, SERVICE_OPEN_COVER, None, (90, 100)),
        ({"rail_min_gap": 10}, PATIO_COVER, SERVICE_CLOSE_COVER, None, (0, 60)),
        ({"rail_min_gap": 10}, PATIO_COVER, SERVICE_SET_COVER_POSITION, 40, (40, 60)),
        ({"rail_min_gap": 10}, PATIO_COVER, SERVICE_SET_COVER_POSITION, 55, (55, 65)),
        ({"rail_min_gap": 10}, PATIO_TOP, SERVICE_OPEN_COVER, None, (20, 100)),
        ({"rail_min_gap": 10}, PATIO_TOP, SERVICE_CLOSE_COVER, None, (0, 10)),
        ({"rail_min_gap": 10}, PATIO_TOP, SERVICE_SET_COVER_POSITION, 70, (20, 70)),
        ({"rail_min_gap": 10}, PATIO_TOP, SERVICE_SET_COVER_POSITION, 25, (15, 25)),
        # Guard off: exactly the request plus where the other rail is.
        ({"collision_guard": False}, PATIO_COVER, SERVICE_OPEN_COVER, None, (100, 60)),
        ({"collision_guard": False}, PATIO_COVER, SERVICE_SET_COVER_POSITION, 80, (80, 60)),
        ({"collision_guard": False}, PATIO_TOP, SERVICE_CLOSE_COVER, None, (20, 0)),
        ({"collision_guard": False}, PATIO_TOP, SERVICE_SET_COVER_POSITION, 10, (20, 10)),
        ({"collision_guard": False, "rail_min_gap": 10}, PATIO_COVER, SERVICE_OPEN_COVER, None, (100, 60)),
    ],
)
async def test_collision_guard(
    hass: HomeAssistant,
    hub: FakeHub,
    options: dict[str, Any],
    entity_id: str,
    service: str,
    position: int | None,
    expected: tuple[int, int],
) -> None:
    _add_patio(hub)
    await _setup(hass, options)

    data = {} if position is None else {ATTR_POSITION: position}
    await _call(hass, service, entity_id, **data)
    assert _rails(_commands_for(hub, PATIO)[-1]) == expected


@pytest.mark.parametrize(
    ("axis", "target", "held", "guard", "gap", "expected"),
    [
        # The hub stores out-of-range values verbatim, so a held value of 250 is possible.
        (AXIS_BOTTOM, 40, 250, True, 0, (40, 100)),
        (AXIS_MIDDLE, 120, -5, True, 0, (0, 100)),
        # A held rail already on the wrong side is moved clear by the guard, and left alone without it.
        (AXIS_BOTTOM, 30, 10, True, 5, (30, 35)),
        (AXIS_MIDDLE, 50, 70, True, 5, (45, 50)),
        (AXIS_BOTTOM, 30, 10, False, 5, (30, 10)),
        # The largest gap still leaves both rails a range.
        (AXIS_BOTTOM, 100, 0, True, 50, (50, 100)),
        (AXIS_MIDDLE, 0, 100, True, 50, (0, 50)),
    ],
)
def test_rail_plan_edges(axis: str, target: int, held: int, guard: bool, gap: int, expected: tuple[int, int]) -> None:
    assert plan_rail_move(axis, target, held, guard=guard, gap=gap) == expected


async def test_two_rail_command_is_refused_while_the_other_rail_is_unknown(hass: HomeAssistant, hub: FakeHub) -> None:
    """Guessing the rail a command does not move would move it; refusing is the only safe answer."""
    _add_patio(hub)
    hub.omit_fields = (MIDDLE, TARGET[MIDDLE])
    entry = await _setup(hass)

    with pytest.raises(HomeAssistantError, match=r"Could not move shade Patio: .*top rail"):
        await _call(hass, SERVICE_OPEN_COVER, PATIO_COVER)
    assert _commands_for(hub, PATIO) == []
    assert hass.states.get(PATIO_TOP).attributes.get(ATTR_CURRENT_POSITION) is None

    hub.omit_fields = (BOTTOM, TARGET[BOTTOM])
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError, match=r"Could not move shade Patio: .*bottom rail"):
        await _call(hass, SERVICE_CLOSE_COVER, PATIO_TOP)
    assert _commands_for(hub, PATIO) == []


@pytest.mark.parametrize(
    ("omitted", "entity_id", "position", "expected"),
    [
        (TARGET[MIDDLE], PATIO_COVER, 40, (40, 60)),  # the top rail held at its position, 60
        (TARGET[BOTTOM], PATIO_TOP, 80, (20, 80)),  # the bottom rail held at its position, 20
    ],
)
async def test_a_rail_whose_target_the_hub_does_not_report_is_held_at_its_position(
    hass: HomeAssistant, hub: FakeHub, omitted: str, entity_id: str, position: int, expected: tuple[int, int]
) -> None:
    _add_patio(hub)
    hub.omit_fields = (omitted,)
    await _setup(hass)

    await _call(hass, SERVICE_SET_COVER_POSITION, entity_id, **{ATTR_POSITION: position})
    assert _rails(_commands_for(hub, PATIO)[-1]) == expected


async def test_after_a_stop_the_other_rail_is_unknown_until_the_hub_reports_where_it_stopped(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The hub keeps the abandoned target for ~35 s after MotorStop; restating it would restart the move."""
    _add_patio(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.stuck = True  # the rails never arrive on their own
    hub.stop_point = 45
    hub.stop_reports_after_polls = 3

    await _call(hass, SERVICE_OPEN_COVER, PATIO_COVER)  # (100, 100): both rails under way
    assert hass.states.get(PATIO_COVER).state == CoverState.OPENING
    assert hass.states.get(PATIO_TOP).state == CoverState.OPENING

    await _call(hass, SERVICE_STOP_COVER, PATIO_TOP)
    assert hub.commands[-1][MOTOR_STOP] == 170
    assert BOTTOM not in hub.commands[-1]
    assert hass.states.get(PATIO_COVER).state == CoverState.OPEN  # no motion flags after our stop
    assert hass.states.get(PATIO_TOP).state == CoverState.OPEN
    assert coordinator.update_interval == MOTION  # still waiting for the stop point

    with pytest.raises(HomeAssistantError, match="bottom rail"):
        await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_TOP, **{ATTR_POSITION: 70})
    with pytest.raises(HomeAssistantError, match="top rail"):
        await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_COVER, **{ATTR_POSITION: 10})
    assert len(_commands_for(hub, PATIO)) == 2  # the open and the stop

    await _poll(hass, freezer, MOTION_TICK)
    await _poll(hass, freezer, MOTION_TICK)  # the hub reports where the shade stopped
    assert hass.states.get(PATIO_COVER).attributes[ATTR_CURRENT_POSITION] == 45
    assert coordinator.update_interval == IDLE

    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_TOP, **{ATTR_POSITION: 70})
    assert _rails(_commands_for(hub, PATIO)[-1]) == (45, 70)


async def test_after_a_stop_a_rail_that_was_not_moving_is_still_held(hass: HomeAssistant, hub: FakeHub) -> None:
    """Only a rail still on its way at the stop is unknown; the hub's target for a settled one is where it is."""
    _add_patio(hub)
    await _setup(hass)
    hub.stuck = True
    hub.stop_reports_after_polls = 3  # the stop point is not reported during this test

    await _call(hass, SERVICE_OPEN_COVER, PATIO_TOP)  # (20, 100): only the top rail moves
    await _call(hass, SERVICE_STOP_COVER, PATIO_TOP)

    with pytest.raises(HomeAssistantError, match="top rail"):
        await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_COVER, **{ATTR_POSITION: 10})
    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_TOP, **{ATTR_POSITION: 80})
    assert [_rails(cmd) for cmd in _commands_for(hub, PATIO) if MOTOR_STOP not in cmd] == [(20, 100), (20, 80)]


async def test_after_a_stop_the_hub_never_reports_the_stall_failsafe_ends_the_wait(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """Without a stop report the rails count as unknown only until MOTION_FAILSAFE_SEC, then as the hub's targets."""
    _add_patio(hub)
    entry = await _setup(hass)
    hub.stuck = True
    hub.stop_reports_after_polls = 10_000

    await _call(hass, SERVICE_OPEN_COVER, PATIO_COVER)  # (100, 100): both rails under way
    await _call(hass, SERVICE_STOP_COVER, PATIO_COVER)
    with pytest.raises(HomeAssistantError, match="top rail"):
        await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_COVER, **{ATTR_POSITION: 10})

    await _poll(hass, freezer, MOTION_FAILSAFE_SEC + 1)
    assert _coordinator(entry).update_interval == IDLE
    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_COVER, **{ATTR_POSITION: 10})
    assert _rails(_commands_for(hub, PATIO)[-1]) == (10, 100)  # the top rail at the target the hub still holds


async def test_stop_on_either_cover_stops_the_shade(hass: HomeAssistant, hub: FakeHub) -> None:
    _add_patio(hub)
    await _setup(hass)
    await _call(hass, SERVICE_STOP_COVER, PATIO_COVER)
    await _call(hass, SERVICE_STOP_COVER, PATIO_TOP)
    assert [cmd.get(MOTOR_STOP) for cmd in _commands_for(hub, PATIO)] == [170, 170]


async def test_each_cover_shows_the_motion_of_its_own_rail(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    _add_patio(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.settle_after_polls = 2

    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_TOP, **{ATTR_POSITION: 30})  # (20, 30)
    assert hass.states.get(PATIO_TOP).state == CoverState.CLOSING
    assert hass.states.get(PATIO_COVER).state == CoverState.OPEN  # its rail is held where it is
    assert coordinator.is_moving(str(PATIO), AXIS_MIDDLE)
    assert not coordinator.is_moving(str(PATIO), AXIS_BOTTOM)
    assert coordinator.update_interval == MOTION

    await _poll(hass, freezer, MOTION_TICK)
    await _poll(hass, freezer, MOTION_TICK)
    assert hass.states.get(PATIO_TOP).state == CoverState.OPEN
    assert hass.states.get(PATIO_TOP).attributes[ATTR_CURRENT_POSITION] == 30
    assert coordinator.update_interval == IDLE

    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_COVER, **{ATTR_POSITION: 50})  # (50, 50): top dragged up
    assert hass.states.get(PATIO_COVER).state == CoverState.OPENING
    assert hass.states.get(PATIO_TOP).state == CoverState.OPENING

    await _poll(hass, freezer, MOTION_TICK)
    await _poll(hass, freezer, MOTION_TICK)
    assert hass.states.get(PATIO_COVER).attributes[ATTR_CURRENT_POSITION] == 50
    assert hass.states.get(PATIO_TOP).attributes[ATTR_CURRENT_POSITION] == 50

    await _call(hass, SERVICE_CLOSE_COVER, PATIO_TOP)  # (0, 0): bottom pushed down
    assert hass.states.get(PATIO_TOP).state == CoverState.CLOSING
    assert hass.states.get(PATIO_COVER).state == CoverState.CLOSING
    await _poll(hass, freezer, MOTION_TICK)
    await _poll(hass, freezer, MOTION_TICK)
    assert hass.states.get(PATIO_TOP).state == CoverState.CLOSED
    assert hass.states.get(PATIO_COVER).state == CoverState.CLOSED


async def test_two_rail_verify_retries_both_rails_once(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    _add_patio(hub)
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_SET_COVER_POSITION, PATIO_TOP, **{ATTR_POSITION: 90})
    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, VERIFY_TICK)
    commands = _commands_for(hub, PATIO)
    assert [is_status_request(cmd) for cmd in commands] == [False, True, False]
    assert [_rails(cmd) for cmd in commands if not is_status_request(cmd)] == [(20, 90), (20, 90)]


async def _until(condition: Callable[[], bool]) -> None:
    """Let other tasks run until ``condition`` holds (without waiting on tasks that are held up on purpose)."""
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_scene_commanding_both_covers_at_once_keeps_both_destinations(hass: HomeAssistant, hub: FakeHub) -> None:
    """The second command restates the first one's pending rail, not the hub's old target.

    The hub holds the first post (``control_gate``) until the second command
    has been planned, as a real round trip and the send spacing do in a scene.
    """
    _add_patio(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.stuck = True
    hub.control_gate = asyncio.Event()

    def _set(entity_id: str, position: int) -> asyncio.Task[Any]:
        return hass.async_create_task(
            hass.services.async_call(
                COVER_DOMAIN,
                SERVICE_SET_COVER_POSITION,
                {ATTR_ENTITY_ID: entity_id, ATTR_POSITION: position},
                blocking=True,
            )
        )

    bottom_call = _set(PATIO_COVER, 40)
    await _until(lambda: len(_commands_for(hub, PATIO)) == 1)  # (40, 60) is in flight
    top_call = _set(PATIO_TOP, 70)
    await _until(lambda: coordinator.get_target(str(PATIO), AXIS_MIDDLE) == 70)  # planned and queued
    assert hub.target(PATIO) == 20  # the hub has not applied the first command yet

    hub.control_gate.set()
    await asyncio.gather(bottom_call, top_call)
    await hass.async_block_till_done()
    assert [_rails(cmd) for cmd in _commands_for(hub, PATIO)] == [(40, 60), (40, 70)]
    assert (hub.target(PATIO), hub.target(PATIO, MIDDLE)) == (40, 70)


async def test_top_cover_slider_is_estimated_on_the_top_rail(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    _add_patio(hub)
    await _setup(hass, {"estimation_enabled": True, "full_travel_time_sec": 10})
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, PATIO_TOP)  # 60 -> 100 takes 4 s at 10 s per full travel
    assert hass.states.get(PATIO_TOP).attributes[ATTR_CURRENT_POSITION] == 60
    await _poll(hass, freezer, 2)
    assert 60 < hass.states.get(PATIO_TOP).attributes[ATTR_CURRENT_POSITION] < 100
    assert hass.states.get(PATIO_COVER).attributes[ATTR_CURRENT_POSITION] == 20

    hub.external_move(PATIO, middle=100)
    await _poll(hass, freezer, MOTION_TICK)
    assert hass.states.get(PATIO_TOP).attributes[ATTR_CURRENT_POSITION] == 100
    assert hass.states.get(PATIO_TOP).state == CoverState.OPEN


# --- tilt mode ----------------------------------------------------------------------


async def test_tilt_mode_off_removes_tilt_but_keeps_the_vanes(hass: HomeAssistant, hub: FakeHub) -> None:
    """With tilt off a sheer shade still restates its vane target, so lifting it fully down keeps the vanes."""
    entry = await _setup(hass, {"tilt_mode": "off"})
    assert _coordinator(entry).options.tilt_mode == "off"

    state = hass.states.get(COVER[KITCHEN])
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes

    await _call(hass, SERVICE_CLOSE_COVER, COVER[KITCHEN])
    assert _rails(hub.commands[-1]) == (0, 50)
    assert hub.position(KITCHEN, MIDDLE) == 50


async def test_tilt_mode_auto_gives_tilt_to_sheer_shades_and_drapes_only(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.add_shade(DEN, "Den", 48, 1, bottom=100)
    _add_patio(hub)
    _add_lounge(hub)
    hub.add_shade(70001, "Studio", 49, 3, bottom=0, middle=40)
    await _setup(hass, {"tilt_mode": "auto"})

    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
    studio = hass.states.get("cover.studio")
    assert studio.attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
    assert studio.attributes[ATTR_CURRENT_TILT_POSITION] == 40
    lounge = hass.states.get(LOUNGE_COVER)
    assert lounge.attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
    assert lounge.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    for entity_id in (DEN_COVER, PATIO_COVER, PATIO_TOP):
        assert hass.states.get(entity_id).attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES


# --- drapes ----------------------------------------------------------------------


async def test_drape_gets_one_cover_with_lift_and_tilt_on_the_hub_scale(hass: HomeAssistant, hub: FakeHub) -> None:
    """A SmartDrape: the draw as the position, the vanes as the hub reports them (50 = open), model named."""
    _add_lounge(hub)
    entry = await _setup(hass)
    registry = er.async_get(hass)

    assert _coordinator(entry).peripherals[str(LOUNGE)].profile == PROFILE_DRAPE
    assert registry.async_get(LOUNGE_COVER).unique_id == f"shadeauto_{hub.thing_name}_{LOUNGE}"
    state = hass.states.get(LOUNGE_COVER)
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
    assert state.attributes[ATTR_CURRENT_POSITION] == 30
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    assert state.attributes["friendly_name"] == "Lounge"
    device = dr.async_get(hass).async_get(registry.async_get(LOUNGE_COVER).device_id)
    assert device.model == "SmartDrape (type 80/1)"
    # One cover, one battery sensor and one low-battery sensor, like any other single-cover shade.
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 3 * len(ALL_UIDS) + 3


async def test_every_drape_draw_restates_the_vanes_the_hub_reports(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A bottom-only command would close the vanes (hub rule), so each draw carries the reported vane position."""
    _add_lounge(hub)
    await _setup(hass)

    await _call(hass, SERVICE_SET_COVER_POSITION, LOUNGE_COVER, **{ATTR_POSITION: 70})
    await _call(hass, SERVICE_CLOSE_COVER, LOUNGE_COVER)
    hub.external_move(LOUNGE, middle=17)  # the hub held the vanes to 17 as the draw finished (rtm)
    await _poll(hass, freezer, DEFAULT_POLL + 1)  # the idle poll picks it up
    await _call(hass, SERVICE_OPEN_COVER, LOUNGE_COVER)

    assert [_rails(cmd) for cmd in _commands_for(hub, LOUNGE)] == [(70, 50), (0, 50), (100, 17)]
    state = hass.states.get(LOUNGE_COVER)
    assert state.attributes[ATTR_CURRENT_POSITION] == 100
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 17


async def test_drape_tilt_commands_restate_the_draw_and_use_the_hub_scale(hass: HomeAssistant, hub: FakeHub) -> None:
    """Open tilt is 50 and close tilt is 0 on the hub's scale; the drape is held where it is, never lowered first."""
    _add_lounge(hub)
    await _setup(hass)

    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 83})
    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)
    await _call(hass, SERVICE_OPEN_COVER_TILT, LOUNGE_COVER)
    await _call(hass, SERVICE_SET_COVER_POSITION, LOUNGE_COVER, **{ATTR_POSITION: 60})
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 100})

    assert [_rails(cmd) for cmd in _commands_for(hub, LOUNGE)] == [(30, 83), (30, 0), (30, 50), (60, 50), (60, 100)]
    state = hass.states.get(LOUNGE_COVER)
    assert state.attributes[ATTR_CURRENT_POSITION] == 60
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 100


async def test_drape_tilt_while_the_drape_is_moving_restates_where_it_is_heading(
    hass: HomeAssistant, hub: FakeHub
) -> None:
    _add_lounge(hub)
    await _setup(hass)
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, LOUNGE_COVER)  # (100, 50), under way
    assert hass.states.get(LOUNGE_COVER).state == CoverState.OPENING
    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)
    assert _rails(_commands_for(hub, LOUNGE)[-1]) == (100, 0)  # not the stale 30
    assert hass.states.get(LOUNGE_COVER).state == CoverState.OPENING  # the draw is still on its way


async def test_drape_stop_and_tilt_stop_both_stop_the_motor(hass: HomeAssistant, hub: FakeHub) -> None:
    _add_lounge(hub)
    await _setup(hass)
    await _call(hass, SERVICE_STOP_COVER, LOUNGE_COVER)
    await _call(hass, SERVICE_STOP_COVER_TILT, LOUNGE_COVER)
    assert [cmd.get(MOTOR_STOP) for cmd in _commands_for(hub, LOUNGE)] == [170, 170]


async def test_drape_motion_and_verify_follow_the_draw_only(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The hub rewrites the vane target to 17 as a draw finishes (rtm); that must not keep the drape moving or retry."""
    _add_lounge(hub)
    entry = await _setup(hass, VERIFY_OPTIONS)
    coordinator = _coordinator(entry)
    hub.settle_after_polls = 2  # the command's own refresh is the first poll

    await _call(hass, SERVICE_SET_COVER_POSITION, LOUNGE_COVER, **{ATTR_POSITION: 80})  # (80, 50)
    assert coordinator.update_interval == MOTION
    assert not coordinator.is_moving(str(LOUNGE), AXIS_MIDDLE)  # the vane axis is not tracked
    await _poll(hass, freezer, MOTION_TICK)  # the draw arrives
    assert coordinator.update_interval == IDLE
    hub.external_move(LOUNGE, middle=17)  # ...and the motor left the vanes at 17, not the 50 we restated
    await _poll(hass, freezer, DEFAULT_POLL + 1)
    assert coordinator.update_interval == IDLE
    assert hass.states.get(LOUNGE_COVER).state == CoverState.OPEN
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 17

    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, REPORT_TICK)
    assert len(_commands_for(hub, LOUNGE)) == 1  # the draw arrived; the vanes are not verified


async def test_drape_verify_retries_a_lost_draw_with_the_vanes_restated(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    _add_lounge(hub)
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_CLOSE_COVER, LOUNGE_COVER)
    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, REPORT_TICK)

    commands = _commands_for(hub, LOUNGE)
    assert [is_status_request(cmd) for cmd in commands] == [False, True, False]
    assert _rails(commands[2]) == (0, 50)


async def test_drape_tilt_mode_off_hides_tilt_but_still_restates_the_vanes(hass: HomeAssistant, hub: FakeHub) -> None:
    _add_lounge(hub)
    await _setup(hass, {"tilt_mode": "off"})

    state = hass.states.get(LOUNGE_COVER)
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes

    await _call(hass, SERVICE_OPEN_COVER, LOUNGE_COVER)
    assert _rails(hub.commands[-1]) == (100, 50)


async def test_drape_tilt_keeps_polling_fast_until_the_hub_reports_the_vanes(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A tilt carries no pending target (the vane axis is not tracked); the settle window keeps the polls fast instead.

    rtm saw ``current_tilt_position`` trail a tilt by up to the 30 s idle
    poll while the draw updated within seconds.
    """
    _add_lounge(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.settle_after_polls = 3  # the command's own refresh is the first poll

    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)  # (30, 0)
    assert coordinator.update_interval == MOTION
    state = hass.states.get(LOUNGE_COVER)
    assert state.state == CoverState.OPEN  # the draw is held where it is; no motion flag for the vanes
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    await _poll(hass, freezer, MOTION_TICK)
    assert coordinator.update_interval == MOTION
    await _poll(hass, freezer, MOTION_TICK)  # the hub reports the vanes
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 0
    assert coordinator.update_interval == IDLE

    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)  # already at 0: nothing to wait for
    assert coordinator.update_interval == IDLE


async def test_drape_vanes_landing_short_of_the_target_still_end_the_fast_polling(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """Sent 0, rtm's drapes report 17: the hub reports end states only, so any change on the axis is the move's end."""
    _add_lounge(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.stuck = True

    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)
    await _poll(hass, freezer, MOTION_TICK)
    assert coordinator.update_interval == MOTION
    hub.external_move(LOUNGE, middle=17)
    await _poll(hass, freezer, MOTION_TICK)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 17
    assert coordinator.update_interval == IDLE


async def test_drape_tilt_the_hub_never_reports_ends_the_fast_polling_after_the_window(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    _add_lounge(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.stuck = True

    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 83})
    assert coordinator.update_interval == MOTION
    await _poll(hass, freezer, UNTRACKED_SETTLE_SEC + 1)
    assert coordinator.update_interval == IDLE
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 50


async def test_drape_stop_during_a_tilt_ends_the_fast_polling(hass: HomeAssistant, hub: FakeHub) -> None:
    _add_lounge(hub)
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    hub.stuck = True

    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)
    assert coordinator.update_interval == MOTION
    await _call(hass, SERVICE_STOP_COVER_TILT, LOUNGE_COVER)
    assert coordinator.update_interval == IDLE


@pytest.mark.parametrize(
    ("vanes", "end_stop", "tilt"),
    [
        # No end stop: the hub's value as it is.
        (0, 0, 0),
        (17, 0, 17),
        (50, 0, 50),
        (100, 0, 100),
        # End stop 17 (rtm's drapes): 17-83 reads as 0-100, 50 stays 50, the hub's other stops fall where they fall.
        (17, 17, 0),
        (33, 17, 24),
        (50, 17, 50),
        (66, 17, 74),
        (83, 17, 100),
        # Outside the reachable range: clamped, not extrapolated.
        (0, 17, 0),
        (100, 17, 100),
        # A landing within POSITION_TOLERANCE of a stop is the stop (rtm's drape settled at 82 for 83).
        (19, 17, 0),
        (82, 17, 100),
        (20, 17, 5),
        # The largest end stop still leaves a range.
        (49, 49, 0),
        (50, 49, 50),
        (51, 49, 100),
    ],
)
def test_drape_tilt_shown_for_the_hubs_vane_value(vanes: int, end_stop: int, tilt: int) -> None:
    assert drape_tilt_to_ha(vanes, end_stop) == tilt


@pytest.mark.parametrize(
    ("vanes", "from_draw", "to_draw", "end_stop", "expected"),
    [
        # Closing an open drape takes vanes at a stop (or within tolerance of it) fully shut; the rest stay put.
        (17, 50, 0, 17, 0),
        (19, 50, 0, 17, 0),
        (83, 50, 0, 17, 100),
        (82, 50, 0, 17, 100),
        (20, 50, 0, 17, 20),
        (33, 50, 0, 17, 33),
        (50, 50, 0, 17, 50),
        # Not a close, not from open, draw unknown, or no end stop: the hub's value as it is.
        (17, 50, 40, 17, 17),
        (17, 0, 0, 17, 17),
        (17, None, 0, 17, 17),
        (17, 50, 0, 0, 17),
    ],
)
def test_drape_vanes_restated_by_a_draw(
    vanes: int, from_draw: int | None, to_draw: int, end_stop: int, expected: int
) -> None:
    assert drape_vanes_for_draw(vanes, from_draw=from_draw, to_draw=to_draw, end_stop=end_stop) == expected


@pytest.mark.parametrize(
    ("tilt", "end_stop", "vanes"),
    [
        (0, 0, 0),
        (50, 0, 50),
        (100, 0, 100),
        (250, 0, 100),
        (0, 17, 17),
        (25, 17, 33),
        (50, 17, 50),
        (75, 17, 67),
        (100, 17, 83),
        (-5, 17, 17),
        (0, 49, 49),
        (100, 49, 51),
    ],
)
def test_drape_tilt_sent_for_home_assistants_value(tilt: int, end_stop: int, vanes: int) -> None:
    assert drape_tilt_to_hub(tilt, end_stop) == vanes


async def test_drape_vane_end_stop_maps_the_reachable_range_onto_0_to_100(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """With the end stop at 17, close tilt sends 17 and reads back 0; the vanes a draw restates stay hub values."""
    _add_lounge(hub)
    await _setup(hass, {"drape_vane_end_stop": 17})
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 50

    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 0
    await _call(hass, SERVICE_SET_COVER_POSITION, LOUNGE_COVER, **{ATTR_POSITION: 60})  # restates 17 as 17
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 100})
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 100
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 25})
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 24  # the hub's stop at 33
    await _call(hass, SERVICE_OPEN_COVER_TILT, LOUNGE_COVER)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 50

    assert [_rails(cmd) for cmd in _commands_for(hub, LOUNGE)] == [(30, 17), (60, 17), (60, 83), (60, 33), (60, 50)]

    hub.external_move(LOUNGE, middle=0)  # the app sent the hub's own 0: shown as closed, not below it
    await _poll(hass, freezer, DEFAULT_POLL + 1)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_drape_vane_end_stop_does_not_apply_while_the_drape_is_fully_closed(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """At a draw of 0 the vanes reach 0 and 100 (rtm), so the hub's scale is shown and sent as it is there."""
    hub.add_shade(LOUNGE, "Lounge", 80, 1, bottom=0, middle=17)  # closed, vanes where an open drape left them
    await _setup(hass, {"drape_vane_end_stop": 17})
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 17

    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 0
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 33})
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 33
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, LOUNGE_COVER, **{ATTR_TILT_POSITION: 100})
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 100
    await _call(hass, SERVICE_CLOSE_COVER, LOUNGE_COVER)  # already closed: the vanes are restated as they are
    assert [_rails(cmd) for cmd in _commands_for(hub, LOUNGE)] == [(0, 0), (0, 33), (0, 100), (0, 100)]

    # The attribute follows the draw the hub reports: the same 17 reads 0 once the drape is open.
    hub.external_move(LOUNGE, bottom=50, middle=17)
    await _poll(hass, freezer, DEFAULT_POLL + 1)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 0
    await _call(hass, SERVICE_CLOSE_COVER_TILT, LOUNGE_COVER)  # open: close tilt is the stop
    assert _rails(hub.commands[-1]) == (50, 17)


@pytest.mark.parametrize(
    ("vanes", "sent"),
    [(17, 0), (19, 0), (83, 100), (82, 100), (33, 33), (50, 50)],
)
async def test_closing_a_drape_with_the_end_stop_set_shuts_vanes_that_were_at_the_stop(
    hass: HomeAssistant, hub: FakeHub, vanes: int, sent: int
) -> None:
    """rtm's evening close: with the option at 17, one close must leave the vanes at 0, not parked at 17."""
    hub.add_shade(LOUNGE, "Lounge", 80, 1, bottom=50, middle=vanes)
    await _setup(hass, {"drape_vane_end_stop": 17})

    await _call(hass, SERVICE_CLOSE_COVER, LOUNGE_COVER)
    assert _rails(hub.commands[-1]) == (0, sent)
    state = hass.states.get(LOUNGE_COVER)
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == sent  # at draw 0 the hub's value is shown as it is

    await _call(hass, SERVICE_SET_COVER_POSITION, LOUNGE_COVER, **{ATTR_POSITION: 40})  # a draw that is not a close
    assert _rails(hub.commands[-1]) == (40, sent)


async def test_closing_a_drape_without_the_end_stop_restates_the_vanes_as_before(
    hass: HomeAssistant, hub: FakeHub
) -> None:
    """With the option at 0 (the default rtm approved) nothing about a close changes."""
    hub.add_shade(LOUNGE, "Lounge", 80, 1, bottom=50, middle=17)
    await _setup(hass)

    await _call(hass, SERVICE_CLOSE_COVER, LOUNGE_COVER)
    assert _rails(hub.commands[-1]) == (0, 17)
    assert hass.states.get(LOUNGE_COVER).attributes[ATTR_CURRENT_TILT_POSITION] == 17


async def test_drape_whose_hub_reports_no_vanes_is_drawn_with_the_0_3_0_payload(
    hass: HomeAssistant, hub: FakeHub
) -> None:
    _add_lounge(hub)
    hub.omit_fields = (MIDDLE, TARGET[MIDDLE])
    await _setup(hass)

    state = hass.states.get(LOUNGE_COVER)
    assert state.attributes.get(ATTR_CURRENT_TILT_POSITION) is None
    await _call(hass, SERVICE_SET_COVER_POSITION, LOUNGE_COVER, **{ATTR_POSITION: 10})
    assert hub.commands[-1][BOTTOM] == 10
    assert MIDDLE not in hub.commands[-1]
