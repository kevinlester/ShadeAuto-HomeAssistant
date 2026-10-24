"""Shims that let the integration run on every supported Home Assistant release.

The supported range is set in ``hacs.json`` (``homeassistant``) and explained
in CONTRIBUTING.md ("Supported Home Assistant versions"). Every shim here is
import-level: it picks the newer API when Home Assistant has it and the older
one otherwise, with no difference in behaviour. Each one names the release
that makes it unnecessary; delete it once the minimum reaches that release.

Do not add a shim that changes behaviour between releases. If a deprecation
would need one, raise the minimum instead.
"""

from __future__ import annotations

from typing import Any, cast

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

# Flow schemas. Home Assistant 2026.10 validates flow forms with probatio and
# only catches probatio's errors; earlier releases use voluptuous. probatio
# mirrors the voluptuous API, so callers use ``vol.Schema`` / ``vol.Required``
# / ``vol.Optional`` either way. Remove when the minimum is 2026.10.
try:
    import probatio as vol
except ImportError:  # pragma: no cover - exercised on Home Assistant < 2026.10
    import voluptuous as vol  # type: ignore[no-redef]

# Remove both helpers below when the minimum is 2026.8.
_HAS_GET_BY_IDENTIFIER = hasattr(dr.DeviceRegistry, "async_get_device_by_identifier")
_HAS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__

__all__ = ["get_device_by_identifier", "link_to_hub", "vol"]


def get_device_by_identifier(
    registry: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> dr.DeviceEntry | None:
    """Return the device of ``config_entry_id`` that carries ``identifier``.

    Home Assistant 2026.8 scoped identifiers to a config entry and replaced
    ``async_get_device(identifiers=...)`` with ``async_get_device_by_identifier``;
    the old call is deprecated there. Earlier releases only have the old call,
    which is global, so the result is filtered to the entry here.
    """
    if _HAS_GET_BY_IDENTIFIER:
        return registry.async_get_device_by_identifier(identifier, config_entry_id)
    else:  # pragma: no cover - HA < 2026.8
        device = registry.async_get_device(identifiers={identifier})
        return device if device is not None and config_entry_id in device.config_entries else None


def link_to_hub(device_info: DeviceInfo, *, hub_device_id: str, hub_identifier: tuple[str, str]) -> None:
    """Make ``device_info`` a child of the hub device.

    Home Assistant 2026.8 links devices by device id (``via_device_id``) and
    deprecates the identifier form (``via_device``, removed in 2027.8). Earlier
    releases only understand ``via_device``.
    """
    if _HAS_VIA_DEVICE_ID:
        device_info["via_device_id"] = hub_device_id
    else:  # pragma: no cover - HA < 2026.8
        # Newer type stubs no longer declare the key, hence the cast.
        cast(dict[str, Any], device_info)["via_device"] = hub_identifier
