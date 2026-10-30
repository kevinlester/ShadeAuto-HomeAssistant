"""Config, reconfigure and options flows."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import ShadeAutoApi, ShadeAutoError
from .compat import vol
from .const import (
    CONF_COLLISION_GUARD,
    CONF_ESTIMATION_ENABLED,
    CONF_FULL_TRAVEL_TIME_SEC,
    CONF_HOST,
    CONF_LOW_BATTERY_THRESHOLD,
    CONF_MOTION_POLL_SEC,
    CONF_POLL_SECONDS,
    CONF_RAIL_MIN_GAP,
    CONF_SEND_SPACING_SEC,
    CONF_TILT_MODE,
    CONF_VERIFY_DELAY_SEC,
    CONF_VERIFY_ENABLED,
    DOMAIN,
    MAX_RAIL_MIN_GAP,
    MAX_SEND_SPACING,
    MIN_VERIFY_DELAY,
    TILT_MODES,
)
from .options import ShadeAutoOptions

HOST_SCHEMA = vol.Schema({vol.Required(CONF_HOST): TextSelector()})


def _unique_id(host: str) -> str:
    return f"shadeauto_{host}"


def _seconds(minimum: float, maximum: float, step: float = 1, *, box: bool = False) -> NumberSelector:
    mode = NumberSelectorMode.BOX if box else NumberSelectorMode.SLIDER
    return NumberSelector(NumberSelectorConfig(min=minimum, max=maximum, step=step, unit_of_measurement="s", mode=mode))


class ShadeAutoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a hub by host name or IP."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ShadeAutoOptionsFlow:
        return ShadeAutoOptionsFlow()

    async def _async_can_connect(self, host: str) -> bool:
        api = ShadeAutoApi(host, async_get_clientsession(self.hass))
        try:
            await api.registration()
        except ShadeAutoError:
            return False
        return True

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(_unique_id(host))
            self._abort_if_unique_id_configured()
            if await self._async_can_connect(host):
                return self.async_create_entry(title=f"ShadeAuto ({host})", data={CONF_HOST: host})
            errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", data_schema=HOST_SCHEMA, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the hub's host, e.g. after it got a new IP address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            new_unique_id = _unique_id(host)
            # Not _abort_if_unique_id_configured(): it would also trip on this
            # entry's own unique id when the host is submitted unchanged.
            if any(
                other.unique_id == new_unique_id and other.entry_id != entry.entry_id
                for other in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            if await self._async_can_connect(host):
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_unique_id,
                    title=f"ShadeAuto ({host})",
                    data_updates={CONF_HOST: host},
                )
            errors["base"] = "cannot_connect"
        schema = self.add_suggested_values_to_schema(HOST_SCHEMA, {CONF_HOST: entry.data.get(CONF_HOST, "")})
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class ShadeAutoOptionsFlow(OptionsFlowWithReload):
    """Tuning options; the entry reloads automatically when they change."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = ShadeAutoOptions.from_mapping(self.config_entry.options)
        schema = vol.Schema(
            {
                vol.Optional(CONF_POLL_SECONDS, default=current.poll_seconds): _seconds(5, 120, box=True),
                vol.Optional(CONF_MOTION_POLL_SEC, default=current.motion_poll_sec): _seconds(1, 10, 0.5),
                vol.Optional(CONF_TILT_MODE, default=current.tilt_mode): SelectSelector(
                    SelectSelectorConfig(
                        options=list(TILT_MODES), translation_key=CONF_TILT_MODE, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(CONF_COLLISION_GUARD, default=current.collision_guard): BooleanSelector(),
                vol.Optional(CONF_RAIL_MIN_GAP, default=current.rail_min_gap): NumberSelector(
                    NumberSelectorConfig(min=0, max=MAX_RAIL_MIN_GAP, step=1, unit_of_measurement="%")
                ),
                vol.Optional(CONF_LOW_BATTERY_THRESHOLD, default=current.low_battery_threshold): NumberSelector(
                    NumberSelectorConfig(min=5, max=50, step=1, unit_of_measurement="%")
                ),
                vol.Optional(CONF_SEND_SPACING_SEC, default=current.send_spacing_sec): _seconds(
                    0.15, MAX_SEND_SPACING, 0.05
                ),
                vol.Optional(CONF_VERIFY_ENABLED, default=current.verify_enabled): BooleanSelector(),
                vol.Optional(CONF_VERIFY_DELAY_SEC, default=current.verify_delay_sec): _seconds(MIN_VERIFY_DELAY, 120),
                vol.Optional(CONF_ESTIMATION_ENABLED, default=current.estimation_enabled): BooleanSelector(),
                vol.Optional(CONF_FULL_TRAVEL_TIME_SEC, default=current.full_travel_time_sec): _seconds(5, 120),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
