"""Identify button, one per shade: jogs the shade so you can tell which one it is."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ShadeAutoError
from .const import AXIS_BOTTOM, IDENTIFY_PAUSE_SEC, IDENTIFY_TAPS
from .coordinator import ShadeAutoCoordinator
from .entity import ShadeAutoEntity

if TYPE_CHECKING:
    from . import ShadeAutoConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ShadeAutoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(ShadeAutoIdentify(coordinator, uid) for uid in coordinator.peripherals)


def identify_goes_up_first(position: int | None) -> bool:
    """Whether the identify twitch should start upward.

    A jog past the shade's bottom limit does nothing, so a fully closed shade
    is nudged up first and then back down. Everything else goes down first
    and back up, which is what the vendor app's jog looks like.
    """
    return position is not None and position <= 0


class ShadeAutoIdentify(ShadeAutoEntity, ButtonEntity):
    """Jogs the shade a few small steps one way and then back, so it twitches in place.

    Uses the hub's fine-tune verbs, which move the motor briefly without
    changing the hub's target, so the coordinator's motion tracking and verify
    are not involved and the shade ends where it started. The jogs go out at
    the command pacing, so a press takes a few seconds to play out.
    """

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ShadeAutoCoordinator, uid: str) -> None:
        super().__init__(coordinator, uid, unique_id_suffix="_identify")

    async def async_press(self) -> None:
        if self.coordinator.is_shade_moving(self._uid):
            raise HomeAssistantError(f"Shade {self.shade_name} is moving; identify it once it has stopped")
        up_first = identify_goes_up_first(self.coordinator.get_position(self._uid, AXIS_BOTTOM))
        try:
            for _ in range(IDENTIFY_TAPS):
                await self.coordinator.api.jog(self._uid, up=up_first)
            await asyncio.sleep(IDENTIFY_PAUSE_SEC)
            for _ in range(IDENTIFY_TAPS):
                await self.coordinator.api.jog(self._uid, up=not up_first)
        except ShadeAutoError as err:
            raise HomeAssistantError(f"Could not identify shade {self.shade_name}: {err}") from err
        # The hub's position for the shade may have shifted a unit or two; read it back.
        await self.coordinator.async_request_refresh()
