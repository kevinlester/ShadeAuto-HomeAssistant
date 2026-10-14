from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ShadeAutoCoordinator

# ThingName embeds an account-specific identifier; redact it from shared diagnostics.
REDACT = {"thing_name"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry (both rail fields per shade)."""
    data = hass.data[DOMAIN][entry.entry_id]
    coord: ShadeAutoCoordinator = data["coordinator"]

    peripherals = {
        uid: {**meta, "is_tdbu": coord.is_tdbu(uid)}
        for uid, meta in coord.peripherals.items()
    }

    return {
        "options": dict(entry.options),
        "thing_name": "**REDACTED**",
        "peripherals": peripherals,
        "status": (coord.data or {}).get("status", {}),
    }
