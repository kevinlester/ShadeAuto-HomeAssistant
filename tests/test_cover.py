"""Cover entity behaviour against the fake hub: lift, vane tilt, stop, errors, slider estimation."""

from __future__ import annotations

from datetime import timedelta

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
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.shadeauto.const import AXIS_BOTTOM, DEFAULT_MOTION_POLL, DEFAULT_POLL, MIN_VERIFY_DELAY
from custom_components.shadeauto.coordinator import ShadeAutoCoordinator

from .conftest import BOTTOM, COVER, DINING, KITCHEN, MIDDLE, MOTOR_STOP, OFFICE, FakeHub, make_entry, setup_integration

ALL_FEATURES = 255  # OPEN|CLOSE|SET_POSITION|STOP|OPEN_TILT|CLOSE_TILT|SET_TILT_POSITION|STOP_TILT
LIFT_ONLY_FEATURES = 15

IDLE = timedelta(seconds=DEFAULT_POLL)
# Ticks one second past the motion poll interval, so the scheduled refresh is due whatever stagger HA added.
MOTION_TICK = DEFAULT_MOTION_POLL + 1
# The shortest delay the options allow (shorter stored values are raised to it).
VERIFY_DELAY = MIN_VERIFY_DELAY
VERIFY_TICK = VERIFY_DELAY + 1
VERIFY_OPTIONS = {"verify_enabled": True, "verify_delay_sec": VERIFY_DELAY}
# A 10 s full travel makes Office's 35 -> 100 lift take 6.5 s: two motion polls to cross it.
ESTIMATION_OPTIONS = {"estimation_enabled": True, "full_travel_time_sec": 10}


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: int) -> None:
    await hass.services.async_call(COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Advance time and let the coordinator run its scheduled refresh (and the slider animation its frame)."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, options: dict[str, object]) -> MockConfigEntry:
    entry = make_entry(options)
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    return entry


def _coordinator(entry: MockConfigEntry) -> ShadeAutoCoordinator:
    return entry.runtime_data.coordinator


def _last(hub: FakeHub) -> dict:
    return hub.commands[-1]


async def test_initial_state_mirrors_hub_positions_and_tilt(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    kitchen = hass.states.get(COVER[KITCHEN])
    assert kitchen.state == CoverState.CLOSED
    assert kitchen.attributes[ATTR_CURRENT_POSITION] == 0
    assert kitchen.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    assert kitchen.attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES
    assert kitchen.attributes[ATTR_DEVICE_CLASS] == "shade"
    assert kitchen.attributes["friendly_name"] == "Kitchen"

    dining = hass.states.get(COVER[DINING])
    assert dining.state == CoverState.OPEN
    assert dining.attributes[ATTR_CURRENT_POSITION] == 100
    assert dining.attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_lift_commands_send_bottom_with_explicit_vane_target(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Every lift command carries MiddleRailPosition so the hub cannot silently close the vanes."""
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[OFFICE], **{ATTR_POSITION: 40})
    cmd = _last(hub)
    assert cmd["PeripheralUID"] == OFFICE
    assert cmd[BOTTOM] == 40
    assert cmd[MIDDLE] == 0  # vanes cannot be open away from the bottom
    assert cmd["ThingName"] == hub.thing_name
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 40

    await _call(hass, SERVICE_OPEN_COVER, COVER[OFFICE])
    assert _last(hub)[BOTTOM] == 100
    assert _last(hub)[MIDDLE] == 0
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 100

    await _call(hass, SERVICE_CLOSE_COVER, COVER[OFFICE])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 0  # the vanes were closed, so closing keeps them closed
    assert hass.states.get(COVER[OFFICE]).state == CoverState.CLOSED


async def test_closing_a_shade_keeps_its_vane_setting(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Kitchen is down with vanes half open; close_cover and set_position 0 must not slam them shut."""
    await _call(hass, SERVICE_CLOSE_COVER, COVER[KITCHEN])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 50

    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 0})
    assert _last(hub)[MIDDLE] == 50
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_TILT_POSITION] == 50


async def test_tilt_commands_lower_the_shade_and_set_the_vanes(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Vanes only work at the bottom, so a tilt request is sent as (bottom 0, middle tilt)."""
    await _call(hass, SERVICE_SET_COVER_TILT_POSITION, COVER[OFFICE], **{ATTR_TILT_POSITION: 75})
    cmd = _last(hub)
    assert cmd[BOTTOM] == 0
    assert cmd[MIDDLE] == 75
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_POSITION] == 0
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 75

    await _call(hass, SERVICE_OPEN_COVER_TILT, COVER[OFFICE])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 100
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_TILT_POSITION] == 100

    await _call(hass, SERVICE_CLOSE_COVER_TILT, COVER[OFFICE])
    assert _last(hub)[MIDDLE] == 0
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_raising_a_shade_closes_its_vanes_as_the_hub_does(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[KITCHEN], **{ATTR_POSITION: 50})
    state = hass.states.get(COVER[KITCHEN])
    assert state.attributes[ATTR_CURRENT_POSITION] == 50
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0


async def test_stop_sends_motor_stop_and_settles_where_the_hub_says(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    hub.stuck = True  # the shade never reaches its target on its own
    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    assert hass.states.get(COVER[KITCHEN]).state == CoverState.OPENING

    await _call(hass, SERVICE_STOP_COVER, COVER[KITCHEN])
    assert _last(hub)[MOTOR_STOP] == 170
    assert BOTTOM not in _last(hub)
    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.CLOSED  # it never moved; the hub re-targeted it to 0
    assert state.attributes[ATTR_CURRENT_POSITION] == 0

    await _call(hass, SERVICE_STOP_COVER_TILT, COVER[KITCHEN])
    assert _last(hub)[MOTOR_STOP] == 170


async def test_hub_rejection_surfaces_as_a_service_error_and_undoes_the_optimistic_state(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected /control names the shade and leaves nothing behind: no motion, no estimate, no verify."""
    entry = await _setup(hass, {**VERIFY_OPTIONS, **ESTIMATION_OPTIONS})
    coordinator = _coordinator(entry)
    hub.reject = True

    with pytest.raises(HomeAssistantError, match=r"Could not move shade Kitchen: .*Error 2"):
        await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    await hass.async_block_till_done()  # the cancelled slider animation writes its last frame
    assert len(hub.commands) == 1

    state = hass.states.get(COVER[KITCHEN])
    assert state.state == CoverState.CLOSED  # not "opening"
    assert state.attributes[ATTR_CURRENT_POSITION] == 0  # the hub's actual, not the commanded 100
    assert not coordinator.is_moving(str(KITCHEN), AXIS_BOTTOM)
    assert coordinator.update_interval == IDLE

    hub.reject = False
    await _poll(hass, freezer, VERIFY_TICK)  # the verify timer was cancelled with the command
    assert len(hub.commands) == 1


async def test_a_rejected_tilt_does_not_leak_its_vane_target_into_the_next_command(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Kitchen's vanes are at 50; a rejected open_tilt (middle 100) must not become the next close's vane target."""
    hub.reject = True
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_OPEN_COVER_TILT, COVER[KITCHEN])
    hub.reject = False

    await _call(hass, SERVICE_CLOSE_COVER, COVER[KITCHEN])
    assert _last(hub)[BOTTOM] == 0
    assert _last(hub)[MIDDLE] == 50
    assert hass.states.get(COVER[KITCHEN]).attributes[ATTR_CURRENT_TILT_POSITION] == 50


async def test_a_failed_stop_names_the_shade(hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry) -> None:
    hub.stuck = True
    await _call(hass, SERVICE_OPEN_COVER, COVER[KITCHEN])
    hub.offline = True

    with pytest.raises(HomeAssistantError, match="Could not stop shade Kitchen"):
        await _call(hass, SERVICE_STOP_COVER, COVER[KITCHEN])
    assert len(hub.commands) == 1  # the stop never reached the hub
    hub.offline = False


async def test_estimation_interpolates_the_slider_then_holds_at_the_target(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """The slider climbs while the lift is believed to be under way and waits at the target for the hub."""
    await _setup(hass, ESTIMATION_OPTIONS)
    hub.stuck = True  # the hub takes the target (100) but never reports the shade arriving

    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[OFFICE], **{ATTR_POSITION: 100})
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 35  # no time has passed under the freezer

    await _poll(hass, freezer, MOTION_TICK)  # 3 s into a 6.5 s travel
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPENING
    assert 35 < state.attributes[ATTR_CURRENT_POSITION] < 100

    await _poll(hass, freezer, MOTION_TICK * 2)  # past the travel time; the hub still reports 35
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 100  # held at the target, not snapped back to 35

    hub.external_move(OFFICE, bottom=100)  # the hub finally reports the end state
    await _poll(hass, freezer, MOTION_TICK)
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 100


async def test_estimate_is_dropped_when_someone_else_re_commands_the_shade(
    hass: HomeAssistant, hub: FakeHub, freezer: FrozenDateTimeFactory
) -> None:
    """Once the hub's target is no longer ours the slider shows the hub's actual, never the climbing estimate."""
    await _setup(hass, ESTIMATION_OPTIONS)
    hub.stuck = True

    await _call(hass, SERVICE_SET_COVER_POSITION, COVER[OFFICE], **{ATTR_POSITION: 100})
    hub.external_command(OFFICE, bottom=0)  # the vendor app sends it back down mid-move

    await _poll(hass, freezer, MOTION_TICK)
    state = hass.states.get(COVER[OFFICE])
    assert state.state == CoverState.CLOSING  # hub target 0 is below the actual 35
    assert state.attributes[ATTR_CURRENT_POSITION] == 35

    await _poll(hass, freezer, MOTION_TICK)  # and it stays there: no estimate toward 0 either
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 35


async def test_tilt_can_be_turned_off_in_options(hass: HomeAssistant, hub: FakeHub) -> None:
    entry = make_entry({"tilt_enabled": False})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    state = hass.states.get(COVER[KITCHEN])
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == LIFT_ONLY_FEATURES
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes
    assert state.attributes[ATTR_CURRENT_POSITION] == 0
