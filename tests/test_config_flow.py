"""Config, reconfigure and options flows."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.const import DOMAIN

from .conftest import HOST, FakeHub

NEW_HOST = "192.168.3.250"


async def test_user_flow_creates_entry(hass: HomeAssistant, hub: FakeHub) -> None:
    """A reachable hub produces an entry keyed by host."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    with patch("custom_components.shadeauto.async_setup_entry", return_value=True) as setup:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": f" {HOST} "})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"ShadeAuto ({HOST})"
    assert result["data"] == {"host": HOST}  # whitespace is stripped
    assert result["result"].unique_id == f"shadeauto_{HOST}"
    assert setup.call_count == 1
    # The flow validated connectivity with the registration endpoint only.
    assert [path for path, _ in hub.requests] == ["registration"]


async def test_user_flow_reports_unreachable_hub(hass: HomeAssistant, hub: FakeHub) -> None:
    """A connection failure shows the form again with a cannot_connect error."""
    hub.offline = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_for_configured_host(
    hass: HomeAssistant, hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    """The same host cannot be added twice."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_moves_entry_to_new_host(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Reconfiguring changes host, unique id and title, then reloads against the new host."""
    hub.add_host(NEW_HOST)
    result = await integration.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW_HOST})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert integration.data == {"host": NEW_HOST}
    assert integration.unique_id == f"shadeauto_{NEW_HOST}"
    assert integration.title == f"ShadeAuto ({NEW_HOST})"
    assert integration.runtime_data.api.host == NEW_HOST


async def test_reconfigure_rejects_unreachable_host(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    hub.add_host(NEW_HOST)
    hub.offline = True
    result = await integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW_HOST})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert integration.data == {"host": HOST}


async def test_reconfigure_aborts_when_host_belongs_to_another_entry(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    other = MockConfigEntry(domain=DOMAIN, unique_id=f"shadeauto_{NEW_HOST}", data={"host": NEW_HOST})
    other.add_to_hass(hass)

    result = await integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": NEW_HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert integration.data == {"host": HOST}


async def test_options_flow_reloads_entry_with_new_values(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Submitting options stores them and reloads the entry so they take effect."""
    result = await hass.config_entries.options.async_init(integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "poll_seconds": 10,
            "low_battery_threshold": 30,
            "send_spacing_sec": 0.2,
            "verify_enabled": False,
            "verify_delay_sec": 5,
            "notification_timeout_sec": 1,
            "estimation_enabled": False,
            "full_travel_time_sec": 20,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert integration.options["poll_seconds"] == 10
    coordinator = integration.runtime_data.coordinator
    assert coordinator.options.poll_seconds == 10
    assert coordinator.options.low_battery_threshold == 30
    assert coordinator.update_interval == timedelta(seconds=10)
    assert coordinator.api.send_spacing_sec == 0.2
