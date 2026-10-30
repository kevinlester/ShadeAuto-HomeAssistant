"""ShadeAuto: local control of Norman shades through the ShadeAuto hub.

Setup wires the pieces together: :class:`ShadeAutoApi` talks HTTP to the hub,
:class:`ShadeAutoCoordinator` polls it and tracks commanded moves, and the
cover/sensor/binary_sensor platforms render one device per shade.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ShadeAutoApi
from .const import CONF_HOST, DOMAIN, MANUFACTURER, PLATFORMS
from .coordinator import ShadeAutoCoordinator
from .options import ShadeAutoOptions


@dataclass(slots=True)
class ShadeAutoRuntimeData:
    """Objects that live for the lifetime of a loaded config entry."""

    api: ShadeAutoApi
    coordinator: ShadeAutoCoordinator


type ShadeAutoConfigEntry = ConfigEntry[ShadeAutoRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: ShadeAutoConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    options = ShadeAutoOptions.from_mapping(entry.options)
    api = ShadeAutoApi(host, async_get_clientsession(hass), send_spacing_sec=options.send_spacing_sec)
    coordinator = ShadeAutoCoordinator(hass, entry, api, options)
    await coordinator.async_config_entry_first_refresh()

    # The hub is a device of its own so each shade can hang off it.
    hub_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"hub_{host}")},
        manufacturer=MANUFACTURER,
        name=f"ShadeAuto Hub ({host})",
        model="Local Hub",
    )
    coordinator.hub_device_id = hub_device.id

    entry.runtime_data = ShadeAutoRuntimeData(api=api, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ShadeAutoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
