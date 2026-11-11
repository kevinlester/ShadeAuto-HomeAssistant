"""The Identify button: jogs a shade one small step and back so it twitches in place."""

from __future__ import annotations

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS, ButtonDeviceClass
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_SET_COVER_POSITION,
)
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.button import identify_goes_up_first
from custom_components.shadeauto.const import (
    ATTR_MOTOR_JOG_DOWN,
    ATTR_MOTOR_JOG_UP,
    AXIS_BOTTOM,
    IDENTIFY_TAPS,
    MOTOR_JOG,
)

from .conftest import BOTTOM, COVER, KITCHEN, OFFICE, SLUG, TARGET, FakeHub, pump

IDENTIFY = {uid: f"button.{slug}_identify" for uid, slug in SLUG.items()}
JOG_FIELDS = ("PeripheralUID", "TaskID", "Timestamp", "ThingName", ATTR_MOTOR_JOG_UP, ATTR_MOTOR_JOG_DOWN)


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause between the two jogs is real time; tests do not wait it out."""
    monkeypatch.setattr("custom_components.shadeauto.button.IDENTIFY_PAUSE_SEC", 0)


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await pump(hass)


def _jogs(hub: FakeHub) -> list[str]:
    """The jog verbs sent, in order; fails if a jog command carried anything else."""
    verbs = []
    for command in hub.commands:
        if ATTR_MOTOR_JOG_UP in command or ATTR_MOTOR_JOG_DOWN in command:
            assert set(command) <= set(JOG_FIELDS), command
            (verb,) = [verb for verb in (ATTR_MOTOR_JOG_UP, ATTR_MOTOR_JOG_DOWN) if verb in command]
            assert command[verb] == MOTOR_JOG
            verbs.append(verb)
    return verbs


@pytest.mark.parametrize(("position", "up_first"), [(None, False), (0, True), (1, False), (35, False), (100, False)])
def test_identify_goes_up_first_only_from_the_bottom(position: int | None, up_first: bool) -> None:
    assert identify_goes_up_first(position) is up_first


async def test_every_shade_gets_an_identify_button(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for uid, entity_id in IDENTIFY.items():
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.entity_category is EntityCategory.CONFIG
        assert entry.unique_id.endswith(f"_{uid}_identify")
        state = hass.states.get(entity_id)
        assert state.attributes["device_class"] == ButtonDeviceClass.IDENTIFY
        assert state.attributes["friendly_name"].endswith(" Identify")


async def test_identify_jogs_down_then_up_and_leaves_the_shade_where_it_was(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Office is at 35: a few steps down, as many up; the hub's target never changes and nothing shows as moving."""
    coordinator = integration.runtime_data.coordinator
    await _press(hass, IDENTIFY[OFFICE])

    assert _jogs(hub) == [ATTR_MOTOR_JOG_DOWN] * IDENTIFY_TAPS + [ATTR_MOTOR_JOG_UP] * IDENTIFY_TAPS
    assert all(int(command["PeripheralUID"]) == OFFICE for command in hub.commands)
    assert hub.position(OFFICE) == 35
    assert hub.target(OFFICE) == 35
    assert hub.shades[OFFICE][TARGET[BOTTOM]] == 35
    assert not coordinator.is_moving(str(OFFICE), AXIS_BOTTOM)
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == 35


async def test_identify_jogs_up_first_on_a_closed_shade(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Kitchen is fully down: a jog down would do nothing, so it goes up and then back."""
    await _press(hass, IDENTIFY[KITCHEN])
    assert _jogs(hub) == [ATTR_MOTOR_JOG_UP] * IDENTIFY_TAPS + [ATTR_MOTOR_JOG_DOWN] * IDENTIFY_TAPS
    assert hub.position(KITCHEN) == 0
    assert hub.target(KITCHEN) == 0


async def test_identify_reads_the_hub_back_after_the_jogs(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """The press ends with a read of the hub, so a unit the shade drifted shows in the cover at once."""
    polls = hub.status_calls
    await _press(hass, IDENTIFY[OFFICE])
    assert hub.status_calls > polls
    assert hass.states.get(COVER[OFFICE]).attributes[ATTR_CURRENT_POSITION] == hub.position(OFFICE)


async def test_identify_is_refused_while_the_shade_is_moving(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    hub.settle_after_polls = 5
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: COVER[OFFICE], ATTR_POSITION: 80},
        blocking=True,
    )
    await pump(hass)
    sent = len(hub.commands)

    with pytest.raises(HomeAssistantError, match="Shade Office is moving"):
        await _press(hass, IDENTIFY[OFFICE])
    assert len(hub.commands) == sent
    assert _jogs(hub) == []


async def test_a_rejected_jog_names_the_shade(hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry) -> None:
    hub.reject = True
    with pytest.raises(HomeAssistantError, match=r"Could not identify shade Office: .*Error 2"):
        await _press(hass, IDENTIFY[OFFICE])
    assert _jogs(hub) == [ATTR_MOTOR_JOG_DOWN]  # nothing more is sent after the first jog is refused
