"""The thin HTTP client in api.py, exercised against the fake hub."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.shadeauto.api import ShadeAutoApi

from .conftest import BOTTOM, HOST, KITCHEN, FakeHub


def _api(hass: HomeAssistant) -> ShadeAutoApi:
    api = ShadeAutoApi(HOST, async_get_clientsession(hass))
    api.send_spacing_sec = 0.0
    return api


async def test_registration_learns_the_thing_name(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    assert api.thing_name is None
    await api.registration()
    assert api.thing_name == hub.thing_name


async def test_get_all_peripheral_needs_registration_first(hass: HomeAssistant, hub: FakeHub) -> None:
    """Without the hub's ThingName the hub answers with an error document and no peripherals."""
    api = _api(hass)
    assert await api.get_all_peripheral() == []

    await api.registration()
    peripherals = await api.get_all_peripheral()
    assert {int(p["PeripheralUID"]) for p in peripherals} == set(hub.shades)
    assert {p["PeripheralName"] for p in peripherals} >= {"Kitchen", "Master Bedroom", "PerfectSheer_38A4"}


async def test_status_returns_one_record_per_shade(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    items = await api.status()
    assert {item["PeripheralUID"] for item in items} == set(hub.shades)
    kitchen = next(item for item in items if item["PeripheralUID"] == KITCHEN)
    assert kitchen[BOTTOM] == 0
    assert kitchen["MiddleRailPosition"] == 50


async def test_control_payload_shape(hass: HomeAssistant, hub: FakeHub) -> None:
    """Control carries the hub's identity, a 31-bit TaskID and strictly increasing timestamps."""
    api = _api(hass)
    await api.registration()

    await api.control(KITCHEN, bottom=40)
    await api.control(str(KITCHEN), bottom=60)

    first, second = hub.commands
    assert first["PeripheralUID"] == KITCHEN
    assert second["PeripheralUID"] == KITCHEN  # numeric strings are sent as ints
    assert first[BOTTOM] == 40
    assert second[BOTTOM] == 60
    assert first["ThingName"] == hub.thing_name
    assert 0 < first["TaskID"] < 2**31
    assert second["Timestamp"] > first["Timestamp"]
    assert hub.position(KITCHEN) == 60
