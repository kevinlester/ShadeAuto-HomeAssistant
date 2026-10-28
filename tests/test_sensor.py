"""Battery sensors and the raw-to-percent mapping."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
import pytest

from custom_components.shadeauto.sensor import _raw_to_percent

from .conftest import KITCHEN, FakeHub, make_entry, setup_integration


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("not-a-number", None),
        (100, 100),
        (55.4, 55),
        (0, 0),
        # The hub reports a percentage (the fixtures show ``BatteryVoltage: 100``),
        # and the 0..100 branch runs first, so small floats are percentages too.
        # The "cell voltage" branch in _raw_to_percent is therefore unreachable;
        # these cases pin the current behaviour until that branch is removed.
        (3.75, 4),
        (4.5, 4),  # Python rounds half to even
        (7, 7),
        (-1, None),
        (150, None),
    ],
)
def test_raw_to_percent(raw: object, expected: int | None) -> None:
    """Values in 0..100 are percentages; anything else is unknown."""
    assert _raw_to_percent(raw) == expected


async def test_low_battery_uses_configured_threshold(hass: HomeAssistant, hub: FakeHub) -> None:
    """The binary sensor compares the percentage with the low_battery_threshold option."""
    hub.shades[KITCHEN]["BatteryVoltage"] = 25
    entry = make_entry({"low_battery_threshold": 30})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    assert hass.states.get(f"sensor.shade_{KITCHEN}_battery").state == "25"
    assert hass.states.get(f"binary_sensor.shade_{KITCHEN}_battery_low").state == "on"
