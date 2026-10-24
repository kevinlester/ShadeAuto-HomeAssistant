"""Parsing of entry.options: defaults, migration of older keys, values outside today's form ranges."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.shadeauto.api import ShadeAutoApi
from custom_components.shadeauto.options import ShadeAutoOptions

from .conftest import HOST


def test_defaults() -> None:
    options = ShadeAutoOptions.from_mapping({})
    assert options == ShadeAutoOptions()
    assert options.tilt_mode == "auto"
    assert options.collision_guard is True
    assert options.rail_min_gap == 0
    assert options.verify_delay_sec == 60
    assert options.push_enabled is True  # off means 0.5.0's polling only
    # kedube/ha-norman measured drops at 1.0-1.2 s between commands.
    assert options.send_spacing_sec == 1.5


async def test_the_api_paces_at_the_default_spacing_unless_told_otherwise(hass: HomeAssistant) -> None:
    assert ShadeAutoApi(HOST, async_get_clientsession(hass)).send_spacing_sec == 1.5


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
