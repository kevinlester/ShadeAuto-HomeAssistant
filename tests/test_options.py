"""Parsing of stored options into ShadeAutoOptions."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest

from custom_components.shadeauto.const import DEFAULT_POLL, DEFAULT_SEND_SPACING
from custom_components.shadeauto.options import ShadeAutoOptions

from .conftest import COVER, KITCHEN, FakeHub, make_entry, setup_integration


def test_defaults_when_nothing_is_stored() -> None:
    assert ShadeAutoOptions.from_mapping({}) == ShadeAutoOptions()


def test_stored_values_are_coerced_to_their_types() -> None:
    options = ShadeAutoOptions.from_mapping({"poll_seconds": "15", "send_spacing_sec": 1, "verify_enabled": 0})
    assert options.poll_seconds == 15
    assert options.send_spacing_sec == 1.0
    assert options.verify_enabled is False


@pytest.mark.parametrize("bad", [None, "fast", [], {}])
def test_unusable_values_fall_back_to_defaults(bad: object, caplog: pytest.LogCaptureFixture) -> None:
    """A corrupted or hand-edited option must not raise; the rest of the mapping is still honoured."""
    with caplog.at_level(logging.WARNING):
        options = ShadeAutoOptions.from_mapping({"poll_seconds": bad, "low_battery_threshold": 33})
    assert options.poll_seconds == DEFAULT_POLL
    assert options.low_battery_threshold == 33
    if bad is not None:
        assert "poll_seconds" in caplog.text


async def test_setup_survives_a_bad_stored_option(hass: HomeAssistant, hub: FakeHub) -> None:
    """The integration loads with defaults instead of failing on an unusable stored option."""
    entry = make_entry({"poll_seconds": "not-a-number", "send_spacing_sec": None})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(COVER[KITCHEN]) is not None
    options = entry.runtime_data.coordinator.options
    assert options.poll_seconds == DEFAULT_POLL
    assert options.send_spacing_sec == DEFAULT_SEND_SPACING
