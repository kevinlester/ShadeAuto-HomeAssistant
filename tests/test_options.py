"""Parsing of entry.options: defaults, migration of older keys, values outside today's form ranges, bad values."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.shadeauto.api import ShadeAutoApi
from custom_components.shadeauto.const import DEFAULT_POLL, DEFAULT_SEND_SPACING
from custom_components.shadeauto.options import ShadeAutoOptions

from .conftest import COVER, HOST, KITCHEN, FakeHub, make_entry, setup_integration


def test_defaults() -> None:
    options = ShadeAutoOptions.from_mapping({})
    assert options == ShadeAutoOptions()
    assert options.tilt_mode == "auto"
    assert options.collision_guard is True
    assert options.rail_min_gap == 0
    assert options.verify_delay_sec == 60
    assert options.push_enabled is False  # opt-in: off means 0.5.0's polling only
    # kedube/ha-norman measured drops at 1.0-1.2 s between commands.
    assert options.send_spacing_sec == 1.5


async def test_the_api_paces_at_the_default_spacing_unless_told_otherwise(hass: HomeAssistant) -> None:
    assert ShadeAutoApi(HOST, async_get_clientsession(hass)).send_spacing_sec == 1.5


def test_stored_values_are_coerced_to_their_types() -> None:
    options = ShadeAutoOptions.from_mapping({"poll_seconds": "15", "send_spacing_sec": 1, "verify_enabled": 0})
    assert options.poll_seconds == 15
    assert options.send_spacing_sec == 1.0
    assert options.verify_enabled is False


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({}, "auto"),
        # 0.4.x's tilt_enabled: False hid tilt everywhere.
        ({"tilt_enabled": False}, "off"),
        ({"tilt_enabled": True}, "auto"),
        ({"tilt_mode": "off"}, "off"),
        ({"tilt_mode": "auto"}, "auto"),
        # A saved tilt_mode wins over a stale tilt_enabled.
        ({"tilt_mode": "auto", "tilt_enabled": False}, "auto"),
        ({"tilt_mode": "off", "tilt_enabled": True}, "off"),
        # Something unreadable falls back to the migration, then to the default.
        ({"tilt_mode": "sideways", "tilt_enabled": False}, "off"),
        ({"tilt_mode": "sideways"}, "auto"),
    ],
)
def test_tilt_mode_and_its_migration_from_tilt_enabled(stored: dict[str, Any], expected: str) -> None:
    assert ShadeAutoOptions.from_mapping(stored).tilt_mode == expected


@pytest.mark.parametrize(("stored", "expected"), [(1, 30), (29.5, 30), (30, 30), (45, 45), (120, 120)])
def test_verify_delay_below_the_form_minimum_is_raised_to_it(stored: float, expected: float) -> None:
    """0.3.x allowed delays from 1 s; the form now starts at 30 and would refuse such a stored default."""
    assert ShadeAutoOptions.from_mapping({"verify_delay_sec": stored}).verify_delay_sec == expected


@pytest.mark.parametrize(("stored", "expected"), [(-5, 0), (0, 0), (10.0, 10), (50, 50), (80, 50)])
def test_rail_gap_is_kept_inside_the_form_range(stored: float, expected: int) -> None:
    assert ShadeAutoOptions.from_mapping({"rail_min_gap": stored}).rail_min_gap == expected


@pytest.mark.parametrize(("stored", "expected"), [(True, True), (False, False), (1, True), (0, False)])
def test_push_enabled(stored: Any, expected: bool) -> None:
    assert ShadeAutoOptions.from_mapping({"push_enabled": stored}).push_enabled is expected


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
