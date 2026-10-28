"""Cover entity behaviour against the fake hub."""

from __future__ import annotations

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import BOTTOM, KITCHEN, MIDDLE, OFFICE, FakeHub

KITCHEN_ENTITY = f"cover.shade_{KITCHEN}"
OFFICE_ENTITY = f"cover.shade_{OFFICE}"


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: int) -> None:
    await hass.services.async_call(COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
    # Let the coordinator's follow-up status reads finish.
    await hass.async_block_till_done()


async def test_set_position_sends_bottom_rail_only(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """set_cover_position becomes a BottomRailPosition command for that shade."""
    await _call(hass, SERVICE_SET_COVER_POSITION, KITCHEN_ENTITY, **{ATTR_POSITION: 40})

    assert len(hub.commands) == 1
    cmd = hub.commands[0]
    assert cmd["PeripheralUID"] == KITCHEN
    assert cmd[BOTTOM] == 40
    assert MIDDLE not in cmd
    assert cmd["ThingName"] == hub.thing_name
    assert 0 < cmd["TaskID"] < 2**31

    assert hub.position(KITCHEN) == 40
    assert hass.states.get(KITCHEN_ENTITY).attributes[ATTR_CURRENT_POSITION] == 40


async def test_open_and_close_use_the_rail_limits(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """open_cover sends 100, close_cover sends 0, and the entity state follows the hub."""
    await _call(hass, SERVICE_OPEN_COVER, OFFICE_ENTITY)
    assert hub.commands[-1] == {**hub.commands[-1], "PeripheralUID": OFFICE, BOTTOM: 100}
    assert hass.states.get(OFFICE_ENTITY).state == "open"
    assert hass.states.get(OFFICE_ENTITY).attributes[ATTR_CURRENT_POSITION] == 100

    await _call(hass, SERVICE_CLOSE_COVER, OFFICE_ENTITY)
    assert hub.commands[-1][BOTTOM] == 0
    assert hass.states.get(OFFICE_ENTITY).state == "closed"
    assert hass.states.get(OFFICE_ENTITY).attributes[ATTR_CURRENT_POSITION] == 0
