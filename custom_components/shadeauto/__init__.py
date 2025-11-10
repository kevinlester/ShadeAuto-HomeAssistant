from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PLATFORMS, CONF_HOST, DEFAULT_POLL
from .api import ShadeAutoApi
from .coordinator import ShadeAutoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    session = async_get_clientsession(hass)
    api = ShadeAutoApi(host, session)

    poll = entry.options.get("poll_seconds", DEFAULT_POLL)
    coordinator = ShadeAutoCoordinator(hass, api, poll)
    await coordinator.async_config_entry_first_refresh()

    # Ensure a hub device exists for via_device references
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"hub_{host}")},
        manufacturer="Norman (ShadeAuto)",
        name=f"ShadeAuto Hub ({host})",
        model="Local Hub",
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"api": api, "coordinator": coordinator, "entry": entry}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
