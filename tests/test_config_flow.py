"""Config, reconfigure and options flows."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.const import DOMAIN

from .conftest import ALL_UIDS, COVER, HOST, KITCHEN, FakeHub, make_entry, setup_integration

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
    """Reconfiguring changes host, unique id and title, then reloads against the new host.

    Devices are keyed on the hub's identity, so the shades keep their devices
    (and with them their areas and names) across the move.
    """
    hub.add_host(NEW_HOST)
    device_registry = dr.async_get(hass)
    kitchen_device_id = er.async_get(hass).async_get(COVER[KITCHEN]).device_id
    hub_device_id = integration.runtime_data.coordinator.hub_device_id

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

    assert er.async_get(hass).async_get(COVER[KITCHEN]).device_id == kitchen_device_id
    assert integration.runtime_data.coordinator.hub_device_id == hub_device_id
    assert device_registry.async_get(hub_device_id).name == f"ShadeAuto Hub ({NEW_HOST})"
    assert len(dr.async_entries_for_config_entry(device_registry, integration.entry_id)) == 1 + len(ALL_UIDS)


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
            "motion_poll_sec": 1.5,
            "tilt_mode": "off",
            "collision_guard": False,
            "rail_min_gap": 10,
            "low_battery_threshold": 30,
            "send_spacing_sec": 2.5,
            "verify_enabled": False,
            "verify_delay_sec": 45,
            "estimation_enabled": False,
            "full_travel_time_sec": 20,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert integration.options["poll_seconds"] == 10
    coordinator = integration.runtime_data.coordinator
    assert coordinator.options.poll_seconds == 10
    assert coordinator.options.motion_poll_sec == 1.5
    assert coordinator.options.tilt_mode == "off"
    assert coordinator.options.collision_guard is False
    assert coordinator.options.rail_min_gap == 10
    assert coordinator.options.low_battery_threshold == 30
    assert coordinator.options.verify_delay_sec == 45
    assert coordinator.update_interval == timedelta(seconds=10)
    assert coordinator.api.send_spacing_sec == 2.5  # above 0.4.x's 1.0 s maximum
    assert "current_tilt_position" not in hass.states.get("cover.kitchen").attributes


# The test entries turn pacing off (0 s), which the form's 0.15 s minimum
# rejects; tests that save the form unchanged store a spacing it accepts.
IN_RANGE_SPACING = {"send_spacing_sec": 0.15}


def _form_defaults(result: Any) -> dict[str, Any]:
    """The options form's fields and their default values."""
    return {str(key): key.default() for key in result["data_schema"].schema}


async def test_options_form_offers_tilt_mode_instead_of_tilt_enabled(hass: HomeAssistant, hub: FakeHub) -> None:
    """An entry saved by 0.4.x with tilt off opens with tilt mode "off", and saving it drops the old key."""
    entry = make_entry({"tilt_enabled": False, **IN_RANGE_SPACING})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = _form_defaults(result)
    assert "tilt_enabled" not in defaults
    assert defaults["tilt_mode"] == "off"
    assert defaults["collision_guard"] is True
    assert defaults["rail_min_gap"] == 0

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["tilt_mode"] == "off"
    assert "tilt_enabled" not in entry.options
    assert "current_tilt_position" not in hass.states.get(COVER[KITCHEN]).attributes


async def test_options_form_can_be_saved_with_a_verify_delay_stored_below_its_minimum(
    hass: HomeAssistant, hub: FakeHub
) -> None:
    """A 0.3.x delay of 10 s would be the form's default and fail its 30 s minimum; it opens as 30 instead."""
    entry = make_entry({"verify_enabled": True, "verify_delay_sec": 10, **IN_RANGE_SPACING})
    entry.add_to_hass(hass)
    await setup_integration(hass, entry)
    assert entry.runtime_data.coordinator.options.verify_delay_sec == 30

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _form_defaults(result)["verify_delay_sec"] == 30

    # Submitting the form unchanged sends every default back, as the frontend does.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["verify_delay_sec"] == 30
    assert entry.runtime_data.coordinator.options.verify_delay_sec == 30
