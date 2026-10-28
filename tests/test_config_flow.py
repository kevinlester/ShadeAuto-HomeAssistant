"""Config flow: adding a hub by host."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.const import DOMAIN

from .conftest import HOST, FakeHub


async def test_user_flow_creates_entry(hass: HomeAssistant, hub: FakeHub) -> None:
    """A reachable hub produces an entry keyed by host."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    with patch("custom_components.shadeauto.async_setup_entry", return_value=True) as setup:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"ShadeAuto ({HOST})"
    assert result["data"] == {"host": HOST}
    assert result["result"].unique_id == f"shadeauto_{HOST}"
    assert setup.call_count == 1
    # The flow validated connectivity with the registration endpoint.
    assert hub.requests == [("registration", hub.requests[0][1])]


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
