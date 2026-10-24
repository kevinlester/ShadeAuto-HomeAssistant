"""Motion tracking: hub as source of truth, adaptive polling, verify-and-retry."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_OPEN_COVER,
    SERVICE_OPEN_COVER_TILT,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
    CoverState,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.shadeauto.const import (
    AXIS_BOTTOM,
    AXIS_MIDDLE,
    DEFAULT_MOTION_POLL,
    DEFAULT_POLL,
    MAX_SEND_SPACING,
    MIN_VERIFY_DELAY,
    MOTION_FAILSAFE_SEC,
    PENDING_GRACE_SEC,
    STATUS_REPORT_WAIT_SEC,
)
from custom_components.shadeauto.coordinator import ShadeAutoCoordinator

from .conftest import (
    ALL_UIDS,
    BOTTOM,
    COVER,
    KITCHEN,
    MIDDLE,
    OFFICE,
    PUSH_OPTIONS,
    STATUS_REQUEST,
    TARGET,
    FakeHub,
    is_status_request,
    make_entry,
    pump,
    setup_integration,
)

IDLE = timedelta(seconds=DEFAULT_POLL)
MOTION = timedelta(seconds=DEFAULT_MOTION_POLL)
# Ticks one second past each interval, so the scheduled refresh is due whatever stagger HA added to it.
IDLE_TICK = DEFAULT_POLL + 1
MOTION_TICK = DEFAULT_MOTION_POLL + 1
# The shortest delay the options allow (shorter stored values are raised to it).
VERIFY_DELAY = MIN_VERIFY_DELAY
VERIFY_TICK = VERIFY_DELAY + 1
VERIFY_OPTIONS = {"verify_enabled": True, "verify_delay_sec": VERIFY_DELAY}
# Before retrying, verify asks the shade to report in and waits for it; without
# the stream (these tests) the wait always runs its full length.
REPORT_TICK = STATUS_REPORT_WAIT_SEC + 1


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Advance time and let the coordinator run its scheduled refresh."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: int) -> None:
    await hass.services.async_call(COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, options: dict[str, object]) -> MockConfigEntry:
    entry = make_entry(options)
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    return entry


def _coordinator(entry: MockConfigEntry) -> ShadeAutoCoordinator:
    return entry.runtime_data.coordinator


async def test_polling_speeds_up_while_a_shade_moves_and_slows_down_after(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    assert coordinator.update_interval == IDLE
    assert not coordinator.any_moving()

    # The vendor app commands Office to 80: the hub's target changes first, the actual later.
    hub.settle_after_polls = 3
    hub.external_command(OFFICE, bottom=80)
    await _poll(hass, freezer, IDLE_TICK)

    assert coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.update_interval == MOTION
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 35  # hub actual, no guessing for external moves

    for _ in range(3):
        await _poll(hass, freezer, MOTION_TICK)

    assert not coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.update_interval == IDLE
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 80


async def test_our_command_is_pending_until_the_hub_reflects_it_then_converges(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    hub.settle_after_polls = 2
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 40})

    # The hub accepted the command (its target is 40) but the shade has not arrived yet.
    assert hub.target(KITCHEN) == 40
    assert coordinator.get_target(str(KITCHEN), AXIS_BOTTOM) == 40
    assert coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _poll(hass, freezer, MOTION_TICK)
    await _poll(hass, freezer, MOTION_TICK)

    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 40
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0  # raised shades have closed vanes


async def test_hub_position_wins_when_a_command_is_never_reflected(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """Regression for the old drift bug: the entity must never keep showing a target the hub ignored."""
    entry = await _setup(hass, {"estimation_enabled": True, "full_travel_time_sec": 10})
    coordinator = _coordinator(entry)

    # A hub that answers OK but changes nothing, not even its own target.
    hub.ignore_commands = True
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 60})
    assert coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)  # pending grace window
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _poll(hass, freezer, PENDING_GRACE_SEC + 1)

    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_POSITION] == 0


async def test_external_move_is_reflected_without_a_retry(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    hub.external_move(KITCHEN, bottom=70, middle=0)
    await _poll(hass, freezer, IDLE_TICK)

    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 70
    assert hub.commands == []


async def test_landing_within_tolerance_counts_as_arrived(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A shade that stops at 99 for a target of 100 is there: no motion, idle polling, no retry."""
    entry = await _setup(hass, VERIFY_OPTIONS)
    coordinator = _coordinator(entry)
    hub.landing_offset = -1

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert hub.position(KITCHEN) == 99
    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert coordinator.update_interval == IDLE
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_POSITION] == 99

    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 1


async def test_verify_retries_exactly_once_when_the_shade_does_not_arrive(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True  # hub accepts (target 100) but the shade never moves

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert len(hub.commands) == 1

    await _poll(hass, freezer, VERIFY_TICK)  # verify fires: not at target, hub targets are still ours ...
    assert len(hub.commands) == 2
    assert hub.commands[1]["PeripheralUID"] == KITCHEN  # ... so it asks this shade to report in
    assert hub.commands[1][STATUS_REQUEST] == 0
    await _poll(hass, freezer, REPORT_TICK)  # the report still has it at 0 -> retry
    assert len(hub.commands) == 3
    assert hub.commands[2][BOTTOM] == 100
    assert hub.commands[2][MIDDLE] == 0

    await _poll(hass, freezer, VERIFY_TICK)  # no second retry, and no second status request
    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 3


async def test_verify_retries_once_when_the_hub_never_took_the_command(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A hub target that never reflected ours means a lost command, not someone else's: retry once."""
    await _setup(hass, VERIFY_OPTIONS)
    hub.ignore_commands = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert hub.target(KITCHEN) == 0  # the hub kept its old targets
    assert hub.target(KITCHEN, MIDDLE) == 50

    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, REPORT_TICK)
    assert [is_status_request(cmd) for cmd in hub.commands] == [False, True, False]
    assert hub.commands[2][BOTTOM] == 100
    assert hub.commands[2][MIDDLE] == 0

    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 3


async def test_verify_does_not_fight_a_lift_command_from_someone_else(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    # Before verify fires, the vendor app sends the shade back down.
    hub.external_command(KITCHEN, bottom=0, middle=100)

    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 1  # we did not re-send our stale command
    assert hub.target(KITCHEN) == 0


async def test_verify_does_not_fight_a_vane_change_from_someone_else(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The bottom target still matches ours; only the vane target was re-commanded. Still hands off."""
    await _setup(hass, VERIFY_OPTIONS)

    await _call(hass, SERVICE_OPEN_COVER_TILT, COVER[KITCHEN])
    assert hub.position(KITCHEN, MIDDLE) == 100
    # The vendor app then sets the vanes to 30.
    hub.external_command(KITCHEN, bottom=0, middle=30)

    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 1
    assert hub.target(KITCHEN, MIDDLE) == 30
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_TILT_POSITION] == 30


async def test_verify_ignores_an_axis_the_hub_does_not_report(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A shade without vane fields in /status arrives on the bottom rail alone."""
    await _setup(hass, VERIFY_OPTIONS)
    hub.omit_fields = (MIDDLE, TARGET[MIDDLE])

    await _call(hass, SERVICE_OPEN_COVER, COVER[OFFICE])
    assert hub.position(OFFICE) == 100

    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 1


async def test_a_newer_command_cancels_the_older_verify(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    half = VERIFY_DELAY / 2
    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, half)
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 20})
    assert len(hub.commands) == 2

    await _poll(hass, freezer, half + 1)  # the first verify would have fired now; it was cancelled
    assert len(hub.commands) == 2
    await _poll(hass, freezer, half)  # the second one fires, asks the shade, and retries the second command
    await _poll(hass, freezer, REPORT_TICK)
    assert [is_status_request(cmd) for cmd in hub.commands] == [False, False, True, False]
    assert hub.commands[3][BOTTOM] == 20


# --- verify asks the shade before it retries ----------------------------------------


@pytest.mark.parametrize("push", [False, True])
async def test_verify_skips_the_retry_when_the_shades_report_shows_it_arrived(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, push: bool
) -> None:
    """The hub missed the end of the move; asked, the shade reports that it got there, so nothing is re-sent."""
    await _setup(hass, {**VERIFY_OPTIONS, "push_enabled": push})
    hub.missed_reports = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING  # the hub still says 0

    await _poll(hass, freezer, VERIFY_TICK)
    await pump(hass)
    (status_request,) = (cmd for cmd in hub.commands if is_status_request(cmd))
    assert status_request["PeripheralUID"] == KITCHEN
    assert status_request[STATUS_REQUEST] == 0
    if not push:
        # Nothing says the shade has reported, so the wait runs its course.
        assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING
        await _poll(hass, freezer, REPORT_TICK)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 100
    assert len(hub.commands) == 2  # the command and one status request; no retry

    await _poll(hass, freezer, VERIFY_TICK)
    assert len(hub.commands) == 2


@pytest.mark.parametrize("push", [False, True])
async def test_verify_still_retries_when_the_report_in_rewrites_the_hubs_target(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, push: bool
) -> None:
    """The hub stored our target but the shade never moved (a lost transmission, docs/HUB_PROTOCOL.md 4.8).

    kedube reports that a report-in sets the hub's target to the shade's
    position. That is not someone else re-commanding the shade, so the one
    retry still goes out, as it did in 0.5.0.
    """
    await _setup(hass, {**VERIFY_OPTIONS, "push_enabled": push})
    hub.stuck = True
    hub.report_in_rewrites_target = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert (hub.target(KITCHEN), hub.target(KITCHEN, MIDDLE)) == (100, 0)  # reflected
    await _poll(hass, freezer, VERIFY_TICK)
    await pump(hass)
    await _poll(hass, freezer, REPORT_TICK)
    assert [(cmd.get(BOTTOM), cmd.get(MIDDLE), cmd.get(STATUS_REQUEST)) for cmd in hub.commands] == [
        (100, 0, None),
        (None, None, 0),
        (100, 0, None),
    ]
    assert hub.target(KITCHEN) == 100  # the retry put our target back

    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, REPORT_TICK)
    assert len(hub.commands) == 3


async def test_verify_does_not_fight_a_command_from_someone_else_during_the_report_wait(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A target that changed to neither ours nor the shade's position is a re-command, whatever the report did."""
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True
    hub.report_in_rewrites_target = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, VERIFY_TICK)
    assert is_status_request(hub.commands[-1])
    hub.external_command(KITCHEN, bottom=50, middle=0)  # the vendor app, while verify waits
    await _poll(hass, freezer, REPORT_TICK)
    assert len(hub.commands) == 2
    assert hub.target(KITCHEN) == 50


async def test_verify_reads_the_shades_report_itself_when_nothing_else_does(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """At the longest verify delay the move counts as stalled and polling is idle, and push is off.

    So the only /status read after the report-in is verify's own; without it
    verify would decide on the old position and re-send to a shade that is there.
    """
    entry = await _setup(hass, {"verify_enabled": True, "verify_delay_sec": 120})
    hub.missed_reports = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, 121)
    assert _coordinator(entry).update_interval == IDLE  # stalled
    calls = hub.status_calls
    await _poll(hass, freezer, REPORT_TICK)
    assert hub.status_calls == calls + 1  # verify's read, and no poll
    assert [is_status_request(cmd) for cmd in hub.commands] == [False, True]
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_POSITION] == 100


async def test_verify_retries_as_soon_as_the_shades_report_shows_it_short(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """With push on the report arrives within seconds, and the retry follows it without the full wait."""
    await _setup(hass, {**VERIFY_OPTIONS, **PUSH_OPTIONS})
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, VERIFY_TICK)
    await pump(hass)
    assert [is_status_request(cmd) for cmd in hub.commands] == [False, True, False]
    assert (hub.commands[2][BOTTOM], hub.commands[2][MIDDLE]) == (100, 0)

    await _poll(hass, freezer, VERIFY_TICK)
    await _poll(hass, freezer, REPORT_TICK)
    assert len(hub.commands) == 3


async def test_verify_decides_when_the_wait_runs_out_even_with_push_on(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A shade that never reports (or a stream that is down) costs the wait and nothing more."""
    await _setup(hass, {**VERIFY_OPTIONS, **PUSH_OPTIONS})
    hub.stuck = True
    hub.silent_status_requests = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, VERIFY_TICK)
    await pump(hass)
    assert len(hub.commands) == 2
    await _poll(hass, freezer, STATUS_REPORT_WAIT_SEC - 1)
    assert len(hub.commands) == 2
    await _poll(hass, freezer, 2)
    assert [is_status_request(cmd) for cmd in hub.commands] == [False, True, False]


async def test_a_failed_status_request_leaves_the_retry_decision_standing(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.shadeauto.coordinator")
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True
    hub.reject_status_requests = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, VERIFY_TICK)  # no wait after a failed request: the retry follows at once
    assert [is_status_request(cmd) for cmd in hub.commands] == [False, True, False]
    assert "status request failed" in caplog.text
    assert "Retry for UID" not in caplog.text


async def test_a_newer_command_during_the_report_wait_wins(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, VERIFY_TICK)
    assert is_status_request(hub.commands[-1])
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 20})  # while verify waits
    await _poll(hass, freezer, REPORT_TICK)
    assert len(hub.commands) == 3  # the open is not re-sent

    await _poll(hass, freezer, VERIFY_TICK)  # the newer command's own verify, status request and retry
    await _poll(hass, freezer, REPORT_TICK)
    assert [(cmd.get(BOTTOM), cmd.get(STATUS_REQUEST)) for cmd in hub.commands] == [
        (100, None),
        (None, 0),
        (20, None),
        (None, 0),
        (20, None),
    ]


@pytest.mark.parametrize("request_fails", [False, True])
async def test_a_newer_command_while_the_status_request_is_in_flight_wins(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, request_fails: bool
) -> None:
    """Whether the status request is answered or fails, a command issued meanwhile is not overwritten by a retry."""
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True
    hub.reject_status_requests = request_fails
    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])

    # Held posts block their tasks, so only spin the loop here (async_block_till_done would wait on them).
    hub.control_gate = asyncio.Event()  # the hub holds the status request in flight
    await _run_queued(hass, freezer, int(VERIFY_TICK))
    assert is_status_request(hub.commands[-1])
    call = hass.async_create_task(
        hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_SET_COVER_POSITION,
            {ATTR_ENTITY_ID: COVER[KITCHEN], ATTR_POSITION: 20},
            blocking=True,
        )
    )
    await _run_queued(hass, freezer, 0)  # registered; its post waits for the status request to finish
    hub.control_gate.set()
    await call
    await _poll(hass, freezer, REPORT_TICK)
    assert [(cmd.get(BOTTOM), cmd.get(STATUS_REQUEST)) for cmd in hub.commands] == [
        (100, None),
        (None, 0),
        (20, None),
    ]


async def test_unload_during_the_report_wait_leaves_nothing_behind(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The wait's timer and the verify die with the entry (verify_cleanup fails the test otherwise)."""
    entry = await _setup(hass, {**VERIFY_OPTIONS, **PUSH_OPTIONS})
    hub.stuck = True
    hub.silent_status_requests = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await _poll(hass, freezer, VERIFY_TICK)
    await pump(hass)
    assert len(hub.commands) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _poll(hass, freezer, REPORT_TICK)
    assert len(hub.commands) == 2


async def test_failed_retry_is_logged_once_and_the_shade_recovers_with_the_hub(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(hass, VERIFY_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert len(hub.commands) == 1

    hub.offline = True
    await _poll(hass, freezer, VERIFY_TICK)  # the verify's refresh and its retry both fail
    assert len(hub.commands) == 1
    assert "Retry for UID" in caplog.text
    assert hass.states.get(COVER[KITCHEN]).state == STATE_UNAVAILABLE

    hub.offline = False
    await _poll(hass, freezer, IDLE_TICK)
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING  # hub target 100, actual 0
    assert len(hub.commands) == 1  # the one retry was spent; nothing more is sent


async def test_unload_right_after_a_command_leaves_no_verify_or_animation_behind(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The verify timer and the slider task die with the entry (verify_cleanup fails the test otherwise)."""
    entry = await _setup(hass, {**VERIFY_OPTIONS, "estimation_enabled": True, "full_travel_time_sec": 10})
    hub.stuck = True

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert len(hub.commands) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    await _poll(hass, freezer, VERIFY_TICK)  # the verify would have fired now
    assert len(hub.commands) == 1


async def test_discard_command_forgets_a_command_that_never_reached_the_hub(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    entry = await _setup(hass, VERIFY_OPTIONS)
    coordinator = _coordinator(entry)
    uid = str(KITCHEN)

    cmd_id = coordinator.register_command(uid, bottom=100, middle=0)
    assert coordinator.is_moving(uid, AXIS_BOTTOM)
    assert coordinator.get_target(uid, AXIS_BOTTOM) == 100
    assert coordinator.update_interval == MOTION

    coordinator.discard_command(uid, cmd_id - 1)  # a stale id is ignored
    assert coordinator.is_moving(uid, AXIS_BOTTOM)

    coordinator.discard_command(uid, cmd_id)
    assert not coordinator.is_moving(uid, AXIS_BOTTOM)
    assert coordinator.get_target(uid, AXIS_BOTTOM) == 0  # back to the hub's target
    assert coordinator.update_interval == IDLE

    await _poll(hass, freezer, VERIFY_TICK)  # the verify timer was cancelled with it
    assert hub.commands == []


async def test_stalled_shade_stops_counting_as_moving_after_the_failsafe(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    hub.stuck = True
    hub.external_command(OFFICE, bottom=90)
    await _poll(hass, freezer, IDLE_TICK)
    assert coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)

    await _poll(hass, freezer, MOTION_FAILSAFE_SEC + 1)
    assert not coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.update_interval == IDLE
    assert hass.states.get(COVER[OFFICE]).state == CoverState.OPEN  # hub actual 35, no motion flags


async def test_polling_stays_fast_after_a_stop_until_the_hub_reports_the_stop_point(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Our stop clears the motion flags at once, but the hub only says where the shade ended ~35 s later."""
    coordinator = _coordinator(integration)
    hub.stuck = True  # the raise never completes on its own
    hub.stop_point = 25
    hub.stop_reports_after_polls = 3  # the refresh right after the stop is the first of them

    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _call(hass, SERVICE_STOP_COVER, COVER[KITCHEN])
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.CLOSED  # hub still reports 0, and no opening/closing flag after our stop
    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert hub.target(KITCHEN) == 100  # the hub has not re-targeted yet
    assert coordinator.update_interval == MOTION

    await _poll(hass, freezer, MOTION_TICK)
    assert hub.position(KITCHEN) == 0
    assert coordinator.update_interval == MOTION

    await _poll(hass, freezer, MOTION_TICK)
    assert hub.position(KITCHEN) == 25
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 25
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0  # vanes closed when stopped above 0
    assert coordinator.update_interval == IDLE


async def test_tilt_axis_is_tracked_independently(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(integration)
    hub.settle_after_polls = 2
    hub.external_command(KITCHEN, bottom=0, middle=100)  # app opens the vanes
    await _poll(hass, freezer, IDLE_TICK)

    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert coordinator.is_moving(str(KITCHEN), AXIS_MIDDLE)
    assert coordinator.update_interval == MOTION
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.CLOSED  # tilt motion is not lift motion

    await _poll(hass, freezer, MOTION_TICK)
    assert not coordinator.is_moving(str(KITCHEN), AXIS_MIDDLE)
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_TILT_POSITION] == 100


async def _run_queued(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int) -> None:
    """Advance time a second at a time, letting the paced sends and the polls run.

    Unlike :func:`_poll` this does not wait for every task to finish: the
    service call is still sleeping for its next send slot.
    """
    for second in range(seconds + 1):
        if second:
            freezer.tick(timedelta(seconds=1))
            async_fire_time_changed(hass)
        for _ in range(20):
            await asyncio.sleep(0)


async def test_a_command_waiting_for_its_send_slot_keeps_its_pending_target(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The grace for the hub to reflect a command runs from its post, not from when it joined the queue.

    At the longest spacing the options allow, the last of five shades moved
    by one call waits 12 s for its slot, longer than PENDING_GRACE_SEC, while
    the motion polls keep running.
    """
    entry = await _setup(hass, {"send_spacing_sec": MAX_SEND_SPACING})
    coordinator = _coordinator(entry)
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: [COVER[uid] for uid in ALL_UIDS], ATTR_POSITION: 60},
        blocking=False,
    )
    await _run_queued(hass, freezer, 0)  # all five register; the first post goes out at once
    assert len(hub.commands) == 1

    # Posts go out at 0, 3, 6, 9 and 12 s; the poll at 11 s is past the grace for all of them.
    await _run_queued(hass, freezer, int(PENDING_GRACE_SEC) + 1)
    assert len(hub.commands) == 4
    assert hub.status_calls > 6  # motion polls ran while the last command waited
    (queued,) = set(ALL_UIDS) - {cmd["PeripheralUID"] for cmd in hub.commands}
    assert hub.target(queued) != 60
    assert coordinator.get_target(str(queued), AXIS_BOTTOM) == 60
    assert coordinator.is_moving(str(queued), AXIS_BOTTOM)
    assert hass.states.get(COVER[queued]).state in (CoverState.OPENING, CoverState.CLOSING)

    await _run_queued(hass, freezer, 2)
    await hass.async_block_till_done()
    assert len(hub.commands) == 5
    for uid in ALL_UIDS:
        assert hass.states.get(COVER[uid]).attributes[ATTR_CURRENT_POSITION] == 60
    assert not coordinator.any_moving()


async def test_the_grace_and_the_verify_delay_start_when_the_post_is_answered(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """Registered but not yet sent: neither the grace nor the verify timer runs until command_sent."""
    entry = await _setup(hass, VERIFY_OPTIONS)
    coordinator = _coordinator(entry)
    uid = str(OFFICE)

    cmd_id = coordinator.register_command(uid, bottom=90, middle=0)
    await _poll(hass, freezer, VERIFY_TICK)  # longer than the grace and the verify delay
    assert coordinator.get_target(uid, AXIS_BOTTOM) == 90  # still waiting for its slot
    assert hub.commands == []  # no verify ran for a command that was never sent

    coordinator.command_sent(uid, cmd_id - 1)  # a stale id is ignored
    await _poll(hass, freezer, PENDING_GRACE_SEC + 1)
    assert coordinator.get_target(uid, AXIS_BOTTOM) == 90

    coordinator.command_sent(uid, cmd_id)  # (the post itself is left out: the hub never took the command)
    await _poll(hass, freezer, PENDING_GRACE_SEC + 1)
    assert coordinator.get_target(uid, AXIS_BOTTOM) == 35  # dropped: back to the hub's target
    await _poll(hass, freezer, VERIFY_TICK - PENDING_GRACE_SEC - 1)
    await _poll(hass, freezer, REPORT_TICK)
    status_request, retry = hub.commands  # the verify's status request, then its one retry
    assert status_request[STATUS_REQUEST] == 0
    assert (retry[BOTTOM], retry[MIDDLE]) == (90, 0)


async def test_a_command_cancelled_in_flight_leaves_no_pending_target_behind(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A stopped script cancels its service call; the cover must not show a move nobody is waiting for."""
    coordinator = _coordinator(integration)
    hub.control_gate = asyncio.Event()  # the hub never answers this post

    call = hass.async_create_task(
        hass.services.async_call(COVER_DOMAIN, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: COVER[OFFICE]}, blocking=True)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(hub.commands) == 1
    assert coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)

    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    hub.control_gate.set()
    await hass.async_block_till_done()
    assert not coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert coordinator.get_target(str(OFFICE), AXIS_BOTTOM) == 35
    assert coordinator.update_interval == IDLE
    assert hass.states.get(COVER[OFFICE]).state == CoverState.OPEN
