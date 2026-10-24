"""Push updates: the /notification listener, its reconnects and recycling, and what it does for the entities.

Time is frozen in these tests: every wait in the listener is a Home Assistant
timer, driven with ``freezer.tick`` + ``async_fire_time_changed``, and
:func:`pump` gives the listener (a background task) its turns of the event
loop without moving time.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_SET_COVER_POSITION,
    CoverState,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.shadeauto.const import (
    DOMAIN,
    NOTIFY_MAX_DURATION_SEC,
    NOTIFY_RETRY_MAX_SEC,
    NOTIFY_RETRY_MIN_SEC,
    NOTIFY_STABLE_SEC,
    REFRESH_COOLDOWN_SEC,
)
from custom_components.shadeauto.coordinator import ShadeAutoCoordinator

from .conftest import (
    BOTTOM,
    COVER,
    FAST_OPTIONS,
    HOST,
    KITCHEN,
    MIDDLE,
    OFFICE,
    PUSH_OPTIONS,
    TARGET,
    FakeHub,
    make_entry,
    pump,
    setup_integration,
)

PUSH_LOGGER = "custom_components.shadeauto.push"


async def _setup(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = make_entry({**PUSH_OPTIONS, **(options or {})})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    await pump(hass)
    return entry


def _coordinator(entry: MockConfigEntry) -> ShadeAutoCoordinator:
    return entry.runtime_data.coordinator


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Advance time, fire the timers that are due, and let the listener act on them."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await pump(hass)


async def _quiet(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the refresh debouncer's cooldown run out, so that the next event is read at once.

    A refresh held back for the end of one cooldown starts another when it
    runs, hence two steps.
    """
    for _ in range(2):
        await _tick(hass, freezer, REFRESH_COOLDOWN_SEC + 1)


async def _spin(hass: HomeAssistant) -> None:
    """Give every task a few turns of the event loop without waiting for them (a /status reply is being held)."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _up(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Keep the open stream up long enough to count as working (NOTIFY_STABLE_SEC)."""
    await _tick(hass, freezer, NOTIFY_STABLE_SEC + 1)


def _stream_opens(hub: FakeHub) -> int:
    return sum(path == "notification" for path, _ in hub.requests)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == PUSH_LOGGER and r.levelno >= logging.WARNING]


def _infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == PUSH_LOGGER and r.levelno == logging.INFO]


# --- connecting -------------------------------------------------------------------


async def test_the_listener_connects_after_setup_and_catches_up_once(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The first refresh comes from setup; the acknowledged stream adds exactly one more, nothing else does."""
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    assert [path for path, _ in hub.requests] == [
        "registration",
        "GetAllPeripheral",
        "status",
        "notification",
        "status",
    ]
    assert coordinator.push_connected
    assert entry.runtime_data.listener is not None
    assert len(hub.streams) == 1

    await _quiet(hass, freezer)
    calls = hub.status_calls
    hub.emit({"Error": "Success."})  # another acknowledgement on an open stream means nothing
    hub.emit({"Status": 1, "PeripheralList": []})  # nor does an event without shades
    hub.emit([1, 2, 3])
    hub.emit({"Something": "else"})
    await pump(hass)
    assert hub.status_calls == calls
    assert coordinator.push_connected


async def test_push_is_on_by_default(hass: HomeAssistant, hub: FakeHub) -> None:
    options = {key: value for key, value in FAST_OPTIONS.items() if key != "push_enabled"}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"shadeauto_{HOST}", data={"host": HOST}, options=options)
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    await pump(hass)
    assert _stream_opens(hub) == 1
    assert _coordinator(entry).push_connected


async def test_push_disabled_opens_no_stream(hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory) -> None:
    """Updates come from polling only, as in 0.5.0."""
    entry = await _setup(hass, {"push_enabled": False})
    assert entry.runtime_data.listener is None
    assert not _coordinator(entry).push_connected

    hub.external_move(OFFICE, bottom=80)
    await _tick(hass, freezer, 10)
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 35  # waits for the poll
    await _tick(hass, freezer, 21)
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 80
    assert _stream_opens(hub) == 0


# --- events -----------------------------------------------------------------------


async def test_an_app_move_shows_without_waiting_for_a_poll(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The vendor app moves Office: its new target, and later its arrival, show as soon as the hub announces them."""
    await _setup(hass)
    await _quiet(hass, freezer)
    hub.stuck = True  # the move ends only when the test says so

    hub.external_command(OFFICE, bottom=80)  # the hub announces the new target
    await pump(hass)  # no time passes: the idle poll is 30 s away
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 35

    await _tick(hass, freezer, 20)  # the shade travels; the hub reports nothing meanwhile
    assert hass.states.get(COVER[OFFICE]).state == CoverState.OPENING

    hub.finish_move(OFFICE)  # the hub announces the end of the move
    await pump(hass)  # the next motion poll is still up to 2 s away
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 80
    assert hub.commands == []


async def test_the_end_of_our_own_move_shows_as_soon_as_the_hub_announces_it(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    hub.stuck = True
    await hass.services.async_call(
        COVER_DOMAIN, SERVICE_SET_COVER_POSITION, {ATTR_ENTITY_ID: COVER[KITCHEN], ATTR_POSITION: 60}, blocking=True
    )
    await pump(hass)
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _tick(hass, freezer, 25)
    calls = hub.status_calls
    hub.finish_move(KITCHEN)
    await pump(hass)
    assert hub.status_calls == calls + 1
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 60
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_events_split_across_reads_or_sharing_one_are_all_handled(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    await _quiet(hass, freezer)
    hub.shades[OFFICE].update({BOTTOM: 10, TARGET[BOTTOM]: 10})  # the hub's state changes; its events follow

    hub.emit_raw(b'{"Status": 1792744766350, "Periph')
    await pump(hass)
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 35  # half an event is no event
    hub.emit_raw(b'eralList": [23500]}{"Status": 1792744766351, "PeripheralList": [12177]}')
    await pump(hass)
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 10


async def test_an_update_time_object_triggers_a_refresh(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """kedube: renames and schedule changes in the app arrive as UpdateTime objects; a plain refresh for now."""
    await _setup(hass)
    await _quiet(hass, freezer)
    calls = hub.status_calls
    hub.emit({"UpdateTime": {"peripheral": 1792744766}, "Timestamp": 1792744766000})
    await pump(hass)
    assert hub.status_calls == calls + 1


async def test_events_inside_the_cooldown_are_read_together_a_moment_later(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The debouncer folds a burst into one /status read, and the last event is never dropped."""
    await _setup(hass)
    await _quiet(hass, freezer)
    calls = hub.status_calls

    hub.external_move(OFFICE, bottom=60)
    await pump(hass)
    assert hub.status_calls == calls + 1  # at once
    hub.external_move(KITCHEN, bottom=40)
    hub.external_move(OFFICE, bottom=20)
    await pump(hass)
    assert hub.status_calls == calls + 1  # held for the cooldown
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 60

    await _tick(hass, freezer, REFRESH_COOLDOWN_SEC)
    assert hub.status_calls == calls + 2  # both read in one go
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 20
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_POSITION] == 40


# --- reconnecting and recycling --------------------------------------------------------


async def test_a_dropped_stream_is_reopened_and_what_was_missed_is_read_at_once(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    await _up(hass, freezer)

    hub.drop_streams()  # EOF: the hub closed it
    await pump(hass)
    assert not coordinator.push_connected
    assert _warnings(caplog) == [
        f"Lost the change notifications from the ShadeAuto hub at {HOST} (the hub ended it); polling continues, "
        "and the stream is reopened in the background"
    ]
    hub.external_move(OFFICE, bottom=90)  # announced to nobody: no stream is open

    await _tick(hass, freezer, NOTIFY_RETRY_MIN_SEC - 1)
    assert _stream_opens(hub) == 1
    await _tick(hass, freezer, 1.5)
    assert _stream_opens(hub) == 2
    assert coordinator.push_connected
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 90  # the catch-up refresh
    assert _infos(caplog) == []  # not until the new stream has stayed up for a while
    await _tick(hass, freezer, NOTIFY_STABLE_SEC)
    assert _infos(caplog) == [f"Change notifications from the ShadeAuto hub at {HOST} are back"]


async def test_back_off_doubles_to_its_cap_and_starts_over_after_a_stream_is_back(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(hass)
    await _up(hass, freezer)
    hub.refuse_streams = True
    hub.break_streams()  # a connection error this time
    await pump(hass)

    # async_fire_time_changed fires timers up to 0.5 s early, hence the 1 s margins.
    opens = _stream_opens(hub)
    for delay in (5, 10, 20, 40, 60, 60):
        await _tick(hass, freezer, delay - 1)
        assert _stream_opens(hub) == opens, delay
        await _tick(hass, freezer, 1)
        opens += 1
        assert _stream_opens(hub) == opens, delay
    assert max(NOTIFY_RETRY_MIN_SEC * 2**n for n in range(5)) > NOTIFY_RETRY_MAX_SEC == 60
    assert len(_warnings(caplog)) == 1  # one warning for the whole outage; the attempts log at debug level
    assert "connection reset by the hub" in _warnings(caplog)[0]
    assert _infos(caplog) == []

    hub.refuse_streams = False
    await _tick(hass, freezer, NOTIFY_RETRY_MAX_SEC)
    assert _stream_opens(hub) == opens + 1
    assert _infos(caplog) == []
    await _up(hass, freezer)
    assert len(_infos(caplog)) == 1

    hub.drop_streams()  # it had stayed up, so the back-off starts over
    await pump(hass)
    await _tick(hass, freezer, NOTIFY_RETRY_MIN_SEC)
    assert _stream_opens(hub) == opens + 2
    assert len(_warnings(caplog)) == 2  # a new outage, a new warning


async def test_a_stream_that_is_acknowledged_and_ends_at_once_keeps_backing_off(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A hub that takes each stream and closes it straight away: one warning, a growing back-off, no info lines."""
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    await _up(hass, freezer)
    hub.close_streams_after_answer = True
    hub.drop_streams()
    await pump(hass)

    opens = _stream_opens(hub)
    for delay in (5, 10, 20, 40, 60, 60):
        await _tick(hass, freezer, delay - 1)
        assert _stream_opens(hub) == opens, delay
        await _tick(hass, freezer, 1)
        opens += 1
        assert _stream_opens(hub) == opens, delay
        assert not coordinator.push_connected
    assert len(_warnings(caplog)) == 1
    assert _infos(caplog) == []

    hub.close_streams_after_answer = False  # the next stream stays open
    await _tick(hass, freezer, NOTIFY_RETRY_MAX_SEC)
    assert _stream_opens(hub) == opens + 1
    assert coordinator.push_connected
    await _up(hass, freezer)
    assert _infos(caplog) == [f"Change notifications from the ShadeAuto hub at {HOST} are back"]
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    ("answer", "accepted"),
    [
        ("Success.", True),  # what the hub writes (docs/HUB_PROTOCOL.md 2.5)
        (0, True),  # every other endpoint's success code
        (2, False),
        ("Busy.", False),
        (None, False),
        (False, False),
    ],
)
async def test_only_a_success_answer_opens_the_stream(
    hass: HomeAssistant,
    hub: FakeHub,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    answer: Any,
    accepted: bool,
) -> None:
    """Any other Error value is a refusal: the stream is closed and retried with back-off, quietly."""
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    hub.stream_answer = {"Error": answer}
    entry = await _setup(hass)
    assert _stream_opens(hub) == 1
    assert _coordinator(entry).push_connected is accepted
    if accepted:
        assert len(hub.streams) == 1
        return
    assert hub.streams == []  # we let go of it
    assert f"ended (the hub answered Error {answer!r}); reopening in 5 s" in caplog.text
    await _tick(hass, freezer, NOTIFY_RETRY_MIN_SEC)
    await _tick(hass, freezer, NOTIFY_RETRY_MIN_SEC * 2)
    assert _stream_opens(hub) == 3
    assert _warnings(caplog) == []  # it never worked, so there is nothing to warn about


async def test_the_stream_is_recycled_every_few_minutes_even_when_healthy(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _setup(hass)
    (first,) = hub.streams

    await _tick(hass, freezer, NOTIFY_MAX_DURATION_SEC - 1)
    assert hub.streams == [first]
    await _tick(hass, freezer, 2)
    assert first.closed  # we let go of it ...
    assert len(hub.streams) == 1  # ... and opened the next one straight away
    assert hub.streams[0] is not first
    assert _coordinator(entry).push_connected
    assert _warnings(caplog) == []


async def test_the_recycle_does_not_cancel_a_status_read_in_flight(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """An event just before the recycle starts a read; closing the stream must not cancel it.

    Home Assistant 2026.10 marks a coordinator whose refresh was cancelled as
    failed, which would show every cover as unavailable until the next read.
    """
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    (first,) = hub.streams
    await _tick(hass, freezer, NOTIFY_MAX_DURATION_SEC - 3)

    hub.status_gate = asyncio.Event()  # the hub holds its reply
    hub.external_move(OFFICE, bottom=60)
    await _spin(hass)
    calls = hub.status_calls
    freezer.tick(timedelta(seconds=4))  # the recycle is due
    async_fire_time_changed(hass)
    await _spin(hass)
    assert first.closed
    assert len(hub.streams) == 1

    hub.status_gate.set()
    hub.status_gate = None
    await pump(hass)
    assert hub.status_calls == calls  # the held read finished; the new stream's catch-up waits for the cooldown
    assert coordinator.last_update_success
    state = hass.states.get(COVER[OFFICE])
    assert state.state != STATE_UNAVAILABLE
    assert state.attributes[ATTR_CURRENT_POSITION] == 60


async def test_a_change_announced_while_a_read_is_in_flight_is_read_after_the_cooldown(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The hub answered the read before the change, so one more read follows within the cooldown.

    Home Assistant 2025.8's debouncer drops a request made while its read
    runs, so the coordinator asks again itself; the result is the same on
    every supported release.
    """
    await _setup(hass)
    await _quiet(hass, freezer)
    hub.status_gate = asyncio.Event()

    hub.external_move(OFFICE, bottom=60)  # read at once; the hub has built its reply (60) and holds it
    await _spin(hass)
    hub.external_move(OFFICE, bottom=20)  # announced while that read is in flight
    await _spin(hass)
    calls = hub.status_calls
    hub.status_gate.set()
    hub.status_gate = None
    await pump(hass)
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 60

    await _tick(hass, freezer, REFRESH_COOLDOWN_SEC)
    assert hub.status_calls == calls + 1
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 20


async def test_a_stream_that_is_never_acknowledged_is_retried_with_back_off(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    hub.acknowledge_streams = False
    entry = await _setup(hass)
    assert not _coordinator(entry).push_connected
    (first,) = hub.streams

    await _tick(hass, freezer, NOTIFY_MAX_DURATION_SEC + 1)
    assert first.closed
    assert hub.streams == []  # not reopened at once: it never worked
    await _tick(hass, freezer, NOTIFY_RETRY_MIN_SEC)
    assert len(hub.streams) == 1


async def test_an_unexpected_error_is_logged_and_the_stream_reopened(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in handling one event must not end push updates for good."""
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    with patch.object(coordinator, "async_shades_reported", side_effect=RuntimeError("boom")):
        hub.announce(KITCHEN)
        await pump(hass)
    assert "Unexpected error while reading the hub's change notifications" in caplog.text
    assert not coordinator.push_connected

    await _tick(hass, freezer, NOTIFY_RETRY_MIN_SEC)
    assert _stream_opens(hub) == 2
    assert coordinator.push_connected


# --- stopping -------------------------------------------------------------------------


async def test_a_read_that_ends_after_unload_schedules_nothing(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A change announced during a read that is still in flight at unload: no follow-up read is scheduled.

    verify_cleanup fails the test if the read leaves a timer behind.
    """
    entry = await _setup(hass)
    await _quiet(hass, freezer)
    hub.status_gate = asyncio.Event()
    hub.external_move(OFFICE, bottom=60)
    await _spin(hass)
    hub.external_move(OFFICE, bottom=20)  # announced while the read is held
    await _spin(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    calls = hub.status_calls
    hub.status_gate.set()
    hub.status_gate = None
    await pump(hass)
    assert hub.status_calls == calls


async def test_unload_closes_the_stream_and_leaves_nothing_running(hass: HomeAssistant, hub: FakeHub) -> None:
    """verify_cleanup fails the test if a task or timer outlives the entry."""
    entry = await _setup(hass)
    coordinator = _coordinator(entry)
    (stream,) = hub.streams

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert stream.closed
    assert not coordinator.push_connected


async def test_unload_during_the_back_off_cancels_its_timer(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    entry = await _setup(hass)
    hub.drop_streams()
    await pump(hass)  # now waiting to reconnect

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _tick(hass, freezer, NOTIFY_RETRY_MAX_SEC)
    assert _stream_opens(hub) == 1


async def test_home_assistant_stopping_ends_the_listener(hass: HomeAssistant, hub: FakeHub) -> None:
    """Home Assistant cancels background tasks when it stops, before it closes the HTTP session."""
    coordinator = _coordinator(await _setup(hass))
    (stream,) = hub.streams

    await hass.async_stop()
    assert stream.closed
    assert not coordinator.push_connected
    assert _stream_opens(hub) == 1


# --- profiles ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_type", "module_detail", "sent"),
    [
        (48, 3, {BOTTOM: 70, MIDDLE: 0}),  # sheer: both rails, as in 0.4.0
        (48, 1, {BOTTOM: 70}),  # single rail: bottom only, as in 0.3.0
        (33, 3, {BOTTOM: 70, MIDDLE: 70}),  # two rails: both, the guard dragging the top rail along
    ],
)
async def test_push_changes_no_command(
    hass: HomeAssistant,
    hub: FakeHub,
    freezer: FrozenDateTimeFactory,
    module_type: int,
    module_detail: int,
    sent: dict[str, int],
) -> None:
    """With push on, every profile sends exactly what 0.5.0 sent and ends where the hub says."""
    hub.add_shade(40001, "Den", module_type, module_detail)
    await _setup(hass, {"verify_enabled": True})
    await hass.services.async_call(
        COVER_DOMAIN, SERVICE_SET_COVER_POSITION, {ATTR_ENTITY_ID: "cover.den", ATTR_POSITION: 70}, blocking=True
    )
    await pump(hass)
    await _tick(hass, freezer, 120)  # past the verify: it finds the shade there and sends nothing

    payloads = [{k: v for k, v in cmd.items() if k in (BOTTOM, MIDDLE)} for cmd in hub.commands]
    assert payloads == [sent]
    state = hass.states.get("cover.den")
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 70
