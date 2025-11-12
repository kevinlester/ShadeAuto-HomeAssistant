from __future__ import annotations
from datetime import timedelta

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PLATFORMS, CONF_HOST, DEFAULT_POLL, DEFAULT_SEND_SPACING
from .api import ShadeAutoApi
from .coordinator import ShadeAutoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    session = async_get_clientsession(hass)
    api = ShadeAutoApi(host, session)
    # apply option-driven spacing without core restart
    try:
        api.send_spacing_sec = float(entry.options.get("send_spacing_sec"))
    except Exception:
        pass

    poll = entry.options.get("poll_seconds", DEFAULT_POLL)
    coordinator = ShadeAutoCoordinator(hass, api, poll)
    coordinator.config_entry = entry  # so the watcher can read options
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

    # --- Hot-apply options now (no core restart) ---
    _apply_options_to_runtime(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload options on-the-fly when user clicks Submit in Configure
    async def _update_listener(hass: HomeAssistant, changed: ConfigEntry) -> None:
        _apply_options_to_runtime(hass, changed)
        # Refresh quickly to pick up new poll timing etc.
        data = hass.data[DOMAIN][changed.entry_id]
        await data["coordinator"].async_request_refresh()

    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded

# --------------- helpers ---------------
def _apply_options_to_runtime(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply all option values to live objects (no restart)."""
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    coordinator = data["coordinator"]

    # send_spacing_sec -> API pacing
    try:
        spacing = float(entry.options.get("send_spacing_sec", DEFAULT_SEND_SPACING))
        setattr(api, "send_spacing_sec", spacing)
    except Exception:
        pass

    # poll_seconds -> coordinator update interval
    try:
        poll = int(entry.options.get("poll_seconds", DEFAULT_POLL))
        coordinator.update_interval = timedelta(seconds=poll)
    except Exception:
        pass

    # Other options (verify/low_battery/notification) are consumed dynamically
    # by cover/sensor/binary_sensor at call/evaluation time.
