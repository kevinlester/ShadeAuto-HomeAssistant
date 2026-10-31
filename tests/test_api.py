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
from custom_components.shadeauto.const import NOTIFY_MAX_BUFFER, REQUEST_TIMEOUT_SEC
from custom_components.shadeauto.util import JsonStreamSplitter

from .conftest import (
    BASE,
    BOTTOM,
    HOST,
    KITCHEN,
    MIDDLE,
    MOTOR_STOP,
    OFFICE,
    STATUS_REQUEST,
    FakeHub,
    StreamingResponse,
)


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


async def test_request_status_asks_one_shade_to_report_in(hass: HomeAssistant, hub: FakeHub) -> None:
    """The vendor app's per-shade refresh: StatusRequest 0 with the shade's id, and nothing moves."""
    api = _api(hass)
    await api.registration()
    reply = await api.request_status(str(KITCHEN))

    (cmd,) = hub.commands
    assert {k: v for k, v in cmd.items() if k not in ("TaskID", "Timestamp")} == {
        "PeripheralUID": KITCHEN,
        "ThingName": hub.thing_name,
        STATUS_REQUEST: 0,
    }
    assert reply[STATUS_REQUEST] == 0
    assert reply["Error"] == 0
    assert (hub.position(KITCHEN), hub.position(KITCHEN, MIDDLE)) == (0, 50)


async def test_request_status_is_paced_with_the_commands(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A report-in is a radio transmission too, so it waits for its slot like any command (real clock)."""
    hub = _TimedHub(aioclient_mock)
    api = _api(hass, send_spacing_sec=0.05)
    await api.request_status(KITCHEN)
    await api.control(KITCHEN, bottom=20)
    first, second = hub.control_times
    assert second - first >= 0.05


@pytest.mark.parametrize("uid", [None, "", 0, "0", -1, "-1", "abc", "12177 "])
async def test_a_status_request_without_a_shade_is_never_sent(hass: HomeAssistant, hub: FakeHub, uid: Any) -> None:
    """Without a PeripheralUID the hub makes StatusRequest hub-wide, which closed a user's shades (kedube issue #5)."""
    with pytest.raises(ValueError, match="StatusRequest"):
        await _api(hass).request_status(uid)
    assert hub.requests == []


async def test_report_battery_level_is_never_sent(hass: HomeAssistant, hub: FakeHub) -> None:
    """Not even through the private send path, and not with a shade id: the hub does not confine it."""
    api = _api(hass)
    with pytest.raises(ValueError, match="ReportBatteryLevel"):
        await api._send_control(KITCHEN, {"ReportBatteryLevel": 0})
    with pytest.raises(ValueError, match="StatusRequest"):
        await api._send_control(None, {STATUS_REQUEST: 0})  # type: ignore[arg-type]
    assert hub.requests == []


def test_request_status_is_the_only_report_request_the_api_offers() -> None:
    """Adding a public call here means checking it against docs/HUB_PROTOCOL.md 4.6 first."""
    public = {name for name, value in vars(ShadeAutoApi).items() if callable(value) and not name.startswith("_")}
    assert public == {
        "registration",
        "get_all_peripheral",
        "status",
        "notification_stream",
        "control",
        "stop",
        "request_status",
    }


# --- the /notification stream --------------------------------------------------

ACK = {"Error": "Success."}


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        # The acknowledgement: unlike every other endpoint's, its Error is a string.
        ([b'{"Error": "Success."}'], [ACK]),
        # One object split over two chunks.
        (
            [b'{"Status": 1792744669337, "Periph', b'eralList": [12177]}'],
            [{"Status": 1792744669337, "PeripheralList": [12177]}],
        ),
        # Two objects in one chunk, with nothing between them.
        (
            [b'{"Error": "Success."}{"Status": 1792744744841, "PeripheralList": [12177]}'],
            [ACK, {"Status": 1792744744841, "PeripheralList": [12177]}],
        ),
        # The end of one object and the whole of the next in one chunk.
        (
            [b'{"Status": 1, "Peripheral', b'List": [1]}{"Status": 2, "PeripheralList": [2]}'],
            [{"Status": 1, "PeripheralList": [1]}, {"Status": 2, "PeripheralList": [2]}],
        ),
        # Whitespace around and between objects, and a chunk of nothing else.
        (
            [b' \r\n{"Status": 1, "PeripheralList": [1]}\n', b"  ", b'\t{"Status": 2, "PeripheralList": [2]}  '],
            [{"Status": 1, "PeripheralList": [1]}, {"Status": 2, "PeripheralList": [2]}],
        ),
        # A UTF-8 character split between chunks.
        ([b'{"UpdateTime": {"peripheral": "Caf\xc3', b'\xa9"}}'], [{"UpdateTime": {"peripheral": "Caf\u00e9"}}]),
    ],
)
def test_stream_splitter(chunks: list[bytes], expected: list[Any]) -> None:
    splitter = JsonStreamSplitter(NOTIFY_MAX_BUFFER)
    items = [item for chunk in chunks for item in splitter.feed(chunk)]
    assert items == expected
    assert splitter.pending == ""


def test_stream_splitter_holds_an_incomplete_object_until_the_rest_arrives() -> None:
    splitter = JsonStreamSplitter(NOTIFY_MAX_BUFFER)
    assert splitter.feed(b'{"Status": 1, ') == []
    assert splitter.pending == '{"Status": 1, '
    assert splitter.feed(b'"PeripheralList": []}') == [{"Status": 1, "PeripheralList": []}]


def test_stream_splitter_gives_up_on_text_that_never_becomes_json() -> None:
    splitter = JsonStreamSplitter(16)
    assert splitter.feed(b"<html>") == []
    with pytest.raises(ValueError, match="no JSON"):
        splitter.feed(b"<body>not a hub</body>")


async def test_notification_stream_yields_objects_as_the_hub_writes_them(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    await api.registration()
    stream = api.notification_stream()

    assert await anext(stream) == ACK
    path, body = hub.requests[-1]
    assert path == "notification"
    assert body["ThingName"] == hub.thing_name

    hub.announce(KITCHEN)
    assert (await anext(stream))["PeripheralList"] == [KITCHEN]

    # Half an object is not an object yet.
    pending = asyncio.ensure_future(anext(stream))
    hub.emit_raw(b'{"UpdateTime": {"peripheral": 1792744766}, "Times')
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pending.done()
    hub.emit_raw(b'tamp": 1792744766000}')
    assert await pending == {"UpdateTime": {"peripheral": 1792744766}, "Timestamp": 1792744766000}

    hub.emit_raw(b'{"Status": 17927')
    hub.drop_streams()  # EOF ends the iterator, even inside an object
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert hub.streams == []  # and the reply was let go


async def test_notification_stream_failures_are_connection_errors(hass: HomeAssistant, hub: FakeHub) -> None:
    api = _api(hass)
    hub.refuse_streams = True
    with pytest.raises(ShadeAutoConnectionError):
        await anext(api.notification_stream())

    hub.refuse_streams = False
    stream = api.notification_stream()
    assert await anext(stream) == ACK
    hub.break_streams()
    with pytest.raises(ShadeAutoConnectionError):
        await anext(stream)
    assert hub.streams == []

    stream = api.notification_stream()
    assert await anext(stream) == ACK
    hub.emit_raw(b"<" * (NOTIFY_MAX_BUFFER + 1))  # not what the hub writes
    with pytest.raises(ShadeAutoConnectionError, match="no JSON"):
        await anext(stream)


async def test_notification_stream_http_error_is_a_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{BASE}/notification", status=404)
    with pytest.raises(ShadeAutoConnectionError):
        await anext(_api(hass).notification_stream())


async def test_notification_stream_times_only_the_connect(hass: HomeAssistant, hub: FakeHub) -> None:
    """A quiet stream is normal, so reading is not timed; connecting is."""

    class _Session:
        timeout: Any = None

        def post(self, url: str, **kwargs: Any) -> StreamingResponse:
            self.timeout = kwargs["timeout"]
            response = StreamingResponse(url, hub)
            hub.streams.append(response)
            response.write(b'{"Error": "Success."}')
            return response

    session = _Session()
    stream = ShadeAutoApi(HOST, session).notification_stream()  # type: ignore[arg-type]
    assert await anext(stream) == ACK
    await stream.aclose()
    assert hub.streams == []
    assert session.timeout.total is None
    assert session.timeout.sock_read is None
    assert session.timeout.connect == REQUEST_TIMEOUT_SEC
