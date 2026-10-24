"""Setup and teardown of the config entry."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shadeauto.compat import get_device_by_identifier
from custom_components.shadeauto.const import DOMAIN

from .conftest import (
    ALL_UIDS,
    BATTERY,
    BATTERY_LOW,
    COVER,
    DINING,
    HOST,
    KITCHEN,
    OFFICE,
    FakeHub,
    setup_integration,
)


async def test_setup_creates_entities_from_hub_status(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Every shade gets a cover, a battery sensor and a low-battery binary sensor, named by the hub."""
    assert integration.state is ConfigEntryState.LOADED
    assert [path for path, _ in hub.requests[:3]] == ["registration", "GetAllPeripheral", "status"]

    for uid in ALL_UIDS:
        assert hass.states.get(COVER[uid]) is not None, uid
        assert hass.states.get(BATTERY[uid]).state == "100"
        assert hass.states.get(BATTERY_LOW[uid]).state == "off"

    kitchen = hass.states.get(COVER[KITCHEN])
    assert kitchen.state == "closed"
    assert kitchen.attributes["current_position"] == 0

    dining = hass.states.get(COVER[DINING])
    assert dining.state == "open"
    assert dining.attributes["current_position"] == 100
    assert dining.attributes["friendly_name"] == "PerfectSheer_38A4"

    assert hass.states.get(COVER[OFFICE]).attributes["current_position"] == 35


async def test_hub_and_shade_devices_are_registered(
    hass: HomeAssistant, hub: FakeHub, integration: MockConfigEntry
) -> None:
    """Devices are keyed on the hub's ThingName, not its host, and each shade hangs off the hub device."""
    registry = dr.async_get(hass)
    hub_device = get_device_by_identifier(registry, (DOMAIN, f"hub_{hub.thing_name}"), integration.entry_id)
    assert hub_device is not None
    assert hub_device.model == "Local Hub"
    assert hub_device.name == f"ShadeAuto Hub ({HOST})"

    shade = get_device_by_identifier(registry, (DOMAIN, f"shade_{hub.thing_name}_{KITCHEN}"), integration.entry_id)
    assert shade is not None
    assert shade.via_device_id == hub_device.id
    assert shade.name == "Kitchen"
    assert shade.model == "PerfectSheer"
    assert shade.sw_version == "2.4.1"
    assert get_device_by_identifier(registry, (DOMAIN, f"shade_{HOST}_{KITCHEN}"), integration.entry_id) is None
    assert len(dr.async_entries_for_config_entry(registry, integration.entry_id)) == 1 + len(ALL_UIDS)


async def test_host_keyed_devices_are_migrated_to_the_hub_identity(
    hass: HomeAssistant, hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    """Devices created by 0.3.x (identified by host) keep their id, area and entities after the upgrade."""
    registry = dr.async_get(hass)
    old_shade = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"shade_{HOST}_{KITCHEN}")},
        name="Kitchen",
    )
    registry.async_update_device(old_shade.id, area_id="kitchen", name_by_user="Kitchen blind")
    old_hub = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"hub_{HOST}")},
        name=f"ShadeAuto Hub ({HOST})",
    )

    await setup_integration(hass, config_entry)

    new_identifier = (DOMAIN, f"shade_{hub.thing_name}_{KITCHEN}")
    shade = get_device_by_identifier(registry, new_identifier, config_entry.entry_id)
    assert shade is not None
    assert shade.id == old_shade.id
    assert shade.identifiers == {new_identifier}
    assert shade.area_id == "kitchen"
    assert shade.name_by_user == "Kitchen blind"
    # The user's device name feeds the entity id, so find the cover by unique id.
    entity_registry = er.async_get(hass)
    cover_id = entity_registry.async_get_entity_id("cover", DOMAIN, f"shadeauto_{hub.thing_name}_{KITCHEN}")
    assert cover_id == "cover.kitchen_blind"
    assert entity_registry.async_get(cover_id).device_id == shade.id

    hub_device = get_device_by_identifier(registry, (DOMAIN, f"hub_{hub.thing_name}"), config_entry.entry_id)
    assert hub_device is not None
    assert hub_device.id == old_hub.id
    assert shade.via_device_id == hub_device.id
    assert config_entry.runtime_data.coordinator.hub_device_id == old_hub.id
    # Nothing was duplicated: one hub plus one device per shade.
    assert len(dr.async_entries_for_config_entry(registry, config_entry.entry_id)) == 1 + len(ALL_UIDS)


async def test_duplicate_host_keyed_devices_do_not_block_setup(
    hass: HomeAssistant, hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    """Two host-keyed devices for one shade (a host change under 0.3.x) migrate the first and leave the other."""
    registry = dr.async_get(hass)
    first = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, f"shade_192.168.1.2_{KITCHEN}")}, name="Kitchen"
    )
    second = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, f"shade_{HOST}_{KITCHEN}")}, name="Kitchen"
    )

    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    new_identifier = (DOMAIN, f"shade_{hub.thing_name}_{KITCHEN}")
    migrated = get_device_by_identifier(registry, new_identifier, config_entry.entry_id)
    assert migrated is not None
    assert migrated.id == first.id
    assert er.async_get(hass).async_get(COVER[KITCHEN]).device_id == first.id
    assert registry.async_get(second.id).identifiers == {(DOMAIN, f"shade_{HOST}_{KITCHEN}")}


async def test_unload_entry(hass: HomeAssistant, integration: MockConfigEntry) -> None:
    """Unloading removes the runtime data and makes entities unavailable."""
    assert await hass.config_entries.async_unload(integration.entry_id)
    await hass.async_block_till_done()

    assert integration.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(COVER[KITCHEN]).state == "unavailable"


async def test_setup_retries_when_hub_is_offline(
    hass: HomeAssistant, hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    """A hub that cannot be reached leaves the entry in retry, not in error."""
    hub.offline = True
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.states.get(COVER[KITCHEN]) is None


async def test_setup_retries_when_peripheral_list_is_an_error_envelope(
    hass: HomeAssistant, hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    """A hub that answers GetAllPeripheral with its error envelope must not load with zero shades."""
    hub.thing_name = "not-the-thing-name-registration-returns"
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert [path for path, _ in hub.requests] == ["registration", "GetAllPeripheral"]
    assert hass.states.get(COVER[KITCHEN]) is None
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)


async def test_runtime_data_holds_api_and_coordinator(hass: HomeAssistant, integration: MockConfigEntry) -> None:
    """Platforms reach the shared objects through entry.runtime_data."""
    runtime = integration.runtime_data
    assert runtime.api.host == HOST
    assert runtime.coordinator.api is runtime.api
    assert set(runtime.coordinator.peripherals) == {str(uid) for uid in ALL_UIDS}
    assert runtime.coordinator.hub_device_id is not None
