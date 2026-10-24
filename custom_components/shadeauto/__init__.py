"""ShadeAuto: local control of Norman shades through the ShadeAuto hub.

Setup wires the pieces together: :class:`ShadeAutoApi` talks HTTP to the hub,
:class:`ShadeAutoCoordinator` polls it and tracks commanded moves,
:class:`NotificationListener` (unless push updates are off) refreshes the
coordinator whenever the hub announces a change, and the
cover/sensor/binary_sensor platforms render one device per shade.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ShadeAutoApi
from .compat import get_device_by_identifier
from .const import CONF_HOST, DOMAIN, MANUFACTURER, PLATFORMS
from .coordinator import ShadeAutoCoordinator
from .entity import hub_device_identifier, shade_device_identifier
from .options import ShadeAutoOptions
from .push import NotificationListener

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ShadeAutoRuntimeData:
    """Objects that live for the lifetime of a loaded config entry."""

    api: ShadeAutoApi
    coordinator: ShadeAutoCoordinator
    listener: NotificationListener | None = None  # None while push updates are off


type ShadeAutoConfigEntry = ConfigEntry[ShadeAutoRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: ShadeAutoConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    options = ShadeAutoOptions.from_mapping(entry.options)
    api = ShadeAutoApi(host, async_get_clientsession(hass), send_spacing_sec=options.send_spacing_sec)
    coordinator = ShadeAutoCoordinator(hass, entry, api, options)
    await coordinator.async_config_entry_first_refresh()

    registry = dr.async_get(hass)
    _async_migrate_device_identifiers(registry, entry, coordinator.identity)

    # The hub is a device of its own so each shade can hang off it.
    hub_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={hub_device_identifier(coordinator.identity)},
        manufacturer=MANUFACTURER,
        name=f"ShadeAuto Hub ({host})",
        model="Local Hub",
    )
    coordinator.hub_device_id = hub_device.id

    entry.runtime_data = ShadeAutoRuntimeData(api=api, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if options.push_enabled:
        # After the first refresh, so the stream only adds to a working setup.
        # A config-entry background task: unload and shutdown cancel it.
        entry.runtime_data.listener = NotificationListener(hass, entry, coordinator)
        entry.runtime_data.listener.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ShadeAutoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _migrated_identifier(identifier: tuple[str, str], identity: str) -> tuple[str, str]:
    """Rewrite a ``hub_<x>`` / ``shade_<x>_<uid>`` identifier onto ``identity``; others pass through."""
    domain, value = identifier
    if domain != DOMAIN:
        return identifier
    kind, _, rest = value.partition("_")
    if kind == "hub":
        return hub_device_identifier(identity)
    if kind == "shade" and "_" in rest:
        # The identity itself may contain underscores (a ThingName does), so
        # the uid is whatever follows the last one.
        return shade_device_identifier(identity, rest.rsplit("_", 1)[1])
    return identifier


@callback
def _async_migrate_device_identifiers(registry: dr.DeviceRegistry, entry: ConfigEntry, identity: str) -> None:
    """Re-key this entry's devices on the hub's stable identity.

    Versions up to 0.3.x identified devices by the hub's host, so changing the
    host in the reconfigure flow created a second set of devices and orphaned
    the originals together with their areas and user-given names. Rewriting
    the identifiers in place keeps the device ids, so areas, names and entity
    attachments survive the upgrade. When two devices would collapse onto one
    identifier (a host-change already happened under the old scheme), the
    first keeps it and the other is left alone rather than deleted.
    """
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        new_identifiers = {_migrated_identifier(identifier, identity) for identifier in device.identifiers}
        if new_identifiers == device.identifiers:
            continue
        taken = [
            existing
            for identifier in new_identifiers
            if (existing := get_device_by_identifier(registry, identifier, entry.entry_id)) is not None
            and existing.id != device.id
        ]
        if taken:
            _LOGGER.warning(
                "Not migrating device %s (%s): %s already belongs to device %s; it can be removed by hand",
                device.id,
                device.name_by_user or device.name,
                sorted(new_identifiers),
                taken[0].id,
            )
            continue
        _LOGGER.debug("Migrating device %s identifiers %s -> %s", device.id, device.identifiers, new_identifiers)
        registry.async_update_device(device.id, new_identifiers=new_identifiers)
