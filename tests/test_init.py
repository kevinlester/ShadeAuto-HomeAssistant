"""Setup and teardown of the config entry."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.const import DOMAIN

from .conftest import ALL_UIDS, DINING, HOST, KITCHEN, OFFICE, FakeHub


async def test_setup_creates_entities_from_hub_status(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Every shade gets a cover, a battery sensor and a low-battery binary sensor."""
    assert integration.state is ConfigEntryState.LOADED
    assert [path for path, _ in hub.requests[:3]] == ["registration", "GetAllPeripheral", "status"]

    for uid in ALL_UIDS:
        assert hass.states.get(f"cover.shade_{uid}") is not None, uid
        assert hass.states.get(f"sensor.shade_{uid}_battery").state == "100"
        assert hass.states.get(f"binary_sensor.shade_{uid}_battery_low").state == "off"

    kitchen = hass.states.get(f"cover.shade_{KITCHEN}")
    assert kitchen.state == "closed"
    assert kitchen.attributes["current_position"] == 0

    dining = hass.states.get(f"cover.shade_{DINING}")
    assert dining.state == "open"
    assert dining.attributes["current_position"] == 100

    assert hass.states.get(f"cover.shade_{OFFICE}").attributes["current_position"] == 35


async def test_hub_device_is_registered(hass: HomeAssistant, integration: MockConfigEntry) -> None:
    """The hub device exists so shades can reference it via ``via_device``."""
    registry = dr.async_get(hass)
    hub_device = registry.async_get_device_by_identifier((DOMAIN, f"hub_{HOST}"), integration.entry_id)
    assert hub_device is not None
    assert hub_device.model == "Local Hub"

    shade = registry.async_get_device_by_identifier((DOMAIN, f"shade_{HOST}_{KITCHEN}"), integration.entry_id)
    assert shade is not None
    assert shade.via_device_id == hub_device.id


async def test_unload_entry(hass: HomeAssistant, integration: MockConfigEntry) -> None:
    """Unloading removes the runtime data and makes entities unavailable."""
    assert await hass.config_entries.async_unload(integration.entry_id)
    await hass.async_block_till_done()

    assert integration.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(f"cover.shade_{KITCHEN}").state == "unavailable"


async def test_setup_retries_when_hub_is_offline(
    hass: HomeAssistant, hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    """A hub that cannot be reached leaves the entry in retry, not in error."""
    hub.offline = True
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.states.get(f"cover.shade_{KITCHEN}") is None


async def test_runtime_data_holds_api_and_coordinator(hass: HomeAssistant, integration: MockConfigEntry) -> None:
    """Platforms reach the shared objects through entry.runtime_data."""
    runtime = integration.runtime_data
    assert runtime.api.host == HOST
    assert runtime.coordinator.api is runtime.api
    assert set(runtime.coordinator.peripherals) == {str(uid) for uid in ALL_UIDS}
    assert runtime.coordinator.hub_device_id is not None
