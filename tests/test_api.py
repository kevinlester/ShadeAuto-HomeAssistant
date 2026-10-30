"""The thin HTTP client in api.py, exercised against the fake hub."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.shadeauto.api import (
    ShadeAutoApi,
    ShadeAutoCommandError,
    ShadeAutoConnectionError,
    ShadeAutoResponseError,
    clamp_position,
)

from .conftest import BASE, BOTTOM, HOST, KITCHEN, MIDDLE, MOTOR_STOP, OFFICE, FakeHub


def _api(hass: HomeAssistant, send_spacing_sec: float = 0.0) -> ShadeAutoApi:
    return ShadeAutoApi(HOST, async_get_clientsession(hass), send_spacing_sec=send_spacing_sec)


def test_clamp_position() -> None:
    assert clamp_position(-5) == 0
    assert clamp_position(250) == 100
    assert clamp_position(42.9) == 42


async def test_transport_errors_become_connection_errors(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.offline = True
    api = _api(hass)
    with pytest.raises(ShadeAutoConnectionError):
        await api.status()
    with pytest.raises(ShadeAutoConnectionError):
        await api.control(KITCHEN, bottom=10)


async def test_non_json_body_is_a_connection_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Something other than the hub answering on the port (a captive portal, say) is not a traceback."""
    aioclient_mock.post(f"{BASE}/status", text="<html>not a hub</html>")
    api = _api(hass)
    with pytest.raises(ShadeAutoConnectionError):
        await api.status()


async def test_registration_learns_the_thing_name(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    assert api.thing_name is None
    await api.registration()
    assert api.thing_name == hub.thing_name


async def test_registration_error_code_raises(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.registration["Error"] = 3
    api = _api(hass)
    with pytest.raises(ShadeAutoResponseError) as excinfo:
        await api.registration()
    assert excinfo.value.code == 3
    assert api.thing_name is None


async def test_get_all_peripheral_needs_registration_first(hass: HomeAssistant, hub: FakeHub) -> None:
    """Without the hub's ThingName the hub answers its error envelope, which must not read as "no shades"."""
    api = _api(hass)
    with pytest.raises(ShadeAutoResponseError) as excinfo:
        await api.get_all_peripheral()
    assert excinfo.value.code == 1
    assert "Invalid JSON" in str(excinfo.value)

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
    assert kitchen[MIDDLE] == 50


async def test_control_payload_shape(hass: HomeAssistant, hub: FakeHub) -> None:
    """Control carries the hub's identity, a 31-bit TaskID and strictly increasing timestamps."""
    api = _api(hass)
    await api.registration()

    reply = await api.control(KITCHEN, bottom=40)
    await api.control(str(KITCHEN), bottom=60, middle=0)

    first, second = hub.commands
    assert first["PeripheralUID"] == KITCHEN
    assert second["PeripheralUID"] == KITCHEN  # numeric strings are sent as ints
    assert first[BOTTOM] == 40
    assert MIDDLE not in first
    assert second[BOTTOM] == 60
    assert second[MIDDLE] == 0
    assert first["ThingName"] == hub.thing_name
    assert 0 < first["TaskID"] < 2**31
    assert second["Timestamp"] > first["Timestamp"]
    assert reply["Error"] == 0
    assert reply[BOTTOM] == 40
    assert hub.position(KITCHEN) == 60


async def test_control_clamps_and_sends_both_axes(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    await api.control(OFFICE, bottom=-10, middle=250)
    assert hub.commands[-1][BOTTOM] == 0
    assert hub.commands[-1][MIDDLE] == 100
    assert hub.position(OFFICE) == 0
    assert hub.position(OFFICE, MIDDLE) == 100


async def test_commands_in_one_tick_get_distinct_ids(hass: HomeAssistant, hub: FakeHub) -> None:
    """Commands queued together still get their own TaskID and a strictly increasing Timestamp."""
    api = _api(hass)
    await asyncio.gather(
        api.control(KITCHEN, bottom=10),
        api.control(OFFICE, bottom=20),
        api.control(KITCHEN, bottom=30),
    )
    task_ids = [cmd["TaskID"] for cmd in hub.commands]
    timestamps = [cmd["Timestamp"] for cmd in hub.commands]
    assert len(task_ids) == 3
    assert len(set(task_ids)) == 3
    assert all(0 < task_id < 2**31 for task_id in task_ids)
    assert timestamps == sorted(timestamps) and len(set(timestamps)) == 3


class _TimedHub(FakeHub):
    """A fake hub that notes when each /control arrived."""

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        self.control_times: list[float] = []
        super().__init__(mocker)

    async def _control(self, method: str, url: Any, data: dict[str, Any] | None) -> AiohttpClientMockResponse:
        self.control_times.append(time.monotonic())
        return await super()._control(method, url, data)


async def test_control_posts_are_spaced(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Back-to-back commands reach the hub at least ``send_spacing_sec`` apart (real clock, no freezer)."""
    hub = _TimedHub(aioclient_mock)
    api = _api(hass, send_spacing_sec=0.05)
    await api.control(KITCHEN, bottom=10)
    await api.control(KITCHEN, bottom=20)
    first, second = hub.control_times
    assert second - first >= 0.05


async def test_hub_error_code_raises_command_error(hass: HomeAssistant, hub: FakeHub) -> None:
    hub.reject = True
    api = _api(hass)
    with pytest.raises(ShadeAutoCommandError) as excinfo:
        await api.control(KITCHEN, bottom=0, middle=100)
    assert excinfo.value.code == 2
    assert excinfo.value.payload[BOTTOM] == 0


async def test_stop_sends_motor_stop_only(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    await api.registration()
    reply = await api.stop(KITCHEN)
    cmd = hub.commands[-1]
    assert cmd[MOTOR_STOP] == 170
    assert BOTTOM not in cmd
    assert reply[MOTOR_STOP] == 170
