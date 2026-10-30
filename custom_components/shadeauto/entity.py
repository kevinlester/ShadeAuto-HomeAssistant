"""Base class shared by every entity that belongs to one shade."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import Peripheral, ShadeAutoCoordinator

# Module type/detail pairs the hub reports for shade families we recognise.
KNOWN_MODELS = {(48, 3): "PerfectSheer"}


def model_name(peripheral: Peripheral) -> str:
    """A readable model string for the device page."""
    key = (peripheral.module_type, peripheral.module_detail)
    if key in KNOWN_MODELS:
        return KNOWN_MODELS[key]
    if peripheral.module_type is None:
        return "Shade"
    return f"Shade (type {peripheral.module_type}/{peripheral.module_detail})"


def hub_device_identifier(identity: str) -> tuple[str, str]:
    """The device-registry identifier of the hub itself; see :func:`shade_device_identifier`."""
    return (DOMAIN, f"hub_{identity}")


def shade_device_identifier(identity: str, uid: str) -> tuple[str, str]:
    """The device-registry identifier of one shade, keyed on the hub's stable identity.

    ``identity`` is :attr:`ShadeAutoCoordinator.identity` (the hub's
    ``ThingName``, else its host), not the host alone, so a reconfigure to a
    new IP address keeps the same devices. ``__init__`` migrates identifiers
    written by older versions to this form.
    """
    return (DOMAIN, f"shade_{identity}_{uid}")


class ShadeAutoEntity(CoordinatorEntity[ShadeAutoCoordinator]):
    """An entity attached to one shade (hub peripheral).

    Entity names follow Home Assistant's device-name convention: the shade's
    device carries the hub's name for it, the main cover entity has no name of
    its own, and the diagnostic entities add a translated suffix. Because of
    that, ``self.name`` is ``None`` on the cover; use :attr:`shade_name` when a
    message needs to say which shade it is about.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str, *, unique_id_suffix: str = "") -> None:
        super().__init__(coordinator)
        self._uid = str(uid)
        self._attr_unique_id = f"shadeauto_{coordinator.identity}_{uid}{unique_id_suffix}"
        peripheral = coordinator.peripherals[self._uid]
        self._attr_device_info = DeviceInfo(
            identifiers={shade_device_identifier(coordinator.identity, self._uid)},
            name=peripheral.name,
            manufacturer=MANUFACTURER,
            model=model_name(peripheral),
        )
        firmware = coordinator.status_for(self._uid).get("FirmwareVersion")
        if firmware:
            self._attr_device_info["sw_version"] = str(firmware)
        if coordinator.hub_device_id is not None:
            self._attr_device_info["via_device_id"] = coordinator.hub_device_id

    @property
    def available(self) -> bool:
        """Available while the hub keeps reporting this shade."""
        return super().available and self._uid in self.coordinator.data.get("status", {})

    @property
    def shade_name(self) -> str:
        """The hub's name for this shade (the device name), for log and error messages."""
        return self.coordinator.peripherals[self._uid].name

    @property
    def _status(self) -> dict[str, Any]:
        """This shade's most recent record from ``/status``."""
        return self.coordinator.status_for(self._uid)
