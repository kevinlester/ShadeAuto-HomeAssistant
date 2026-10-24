"""Battery sensors and the raw-to-percent mapping."""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.util import battery_percent, to_int

from .conftest import BATTERY, BATTERY_LOW, KITCHEN, FakeHub, make_entry, setup_integration


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("not-a-number", None),
        (True, None),
        (100, 100),
        ("100", 100),
        (55.4, 55),
        (0, 0),
        # The hub reports a percentage; anything outside 0-100 is unknown.
        (-1, None),
        (150, None),
    ],
)
def test_battery_percent(raw: object, expected: int | None) -> None:
    assert battery_percent(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("48", 48), (3, 3), (3.9, 3), ("x", None), (True, None), ([], None)],
)
def test_to_int(raw: object, expected: int | None) -> None:
    assert to_int(raw) == expected


async def test_battery_entities_are_diagnostic_and_named_after_the_shade(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    battery = registry.async_get(BATTERY[KITCHEN])
    assert battery is not None
    assert battery.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(battery.entity_id).attributes["friendly_name"] == "Kitchen Battery"

    low = registry.async_get(BATTERY_LOW[KITCHEN])
    assert low is not None
    assert low.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(low.entity_id).attributes["friendly_name"] == "Kitchen Battery low"


async def test_low_battery_uses_configured_threshold(hass: HomeAssistant, hub: FakeHub) -> None:
    """The binary sensor compares the percentage with the low_battery_threshold option."""
    hub.shades[KITCHEN]["BatteryVoltage"] = 25
    entry = make_entry({"low_battery_threshold": 30})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    assert hass.states.get(BATTERY[KITCHEN]).state == "25"
    assert hass.states.get(BATTERY_LOW[KITCHEN]).state == "on"


async def test_unknown_battery_value_gives_unknown_states(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.shades[KITCHEN]["BatteryVoltage"] = "n/a"
    entry = make_entry()
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    assert hass.states.get(BATTERY[KITCHEN]).state == "unknown"
    assert hass.states.get(BATTERY_LOW[KITCHEN]).state == "unknown"
