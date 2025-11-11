from __future__ import annotations

from typing import Any
from homeassistant.helpers.selector import selector
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN, CONF_HOST,
    DEFAULT_POLL, DEFAULT_BURST_INTERVAL, DEFAULT_BURST_CYCLES, DEFAULT_LOW_BATT,
    DEFAULT_SEND_SPACING, DEFAULT_VERIFY_ENABLED, DEFAULT_VERIFY_DELAY
)
from .api import ShadeAutoApi

STEP_USER = vol.Schema({vol.Required(CONF_HOST): str})


class ShadeAutoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            api = ShadeAutoApi(host, async_get_clientsession(self.hass))
            try:
                await api.registration()
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"shadeauto_{host}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"ShadeAuto ({host})", data={CONF_HOST: host})

        return self.async_show_form(step_id="user", data_schema=STEP_USER, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ShadeAutoOptionsFlow(config_entry)


class ShadeAutoOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        schema = vol.Schema({
            vol.Optional("poll_seconds", default=self.entry.options.get("poll_seconds", DEFAULT_POLL)):
                selector({"number": {"min": 5, "max": 120, "step": 1, "unit_of_measurement": "s", "mode": "box"}}),

            vol.Optional("burst_interval", default=self.entry.options.get("burst_interval", DEFAULT_BURST_INTERVAL)):
                selector({"number": {"min": 0.5, "max": 10, "step": 0.1, "unit_of_measurement": "s"}}),

            vol.Optional("burst_cycles", default=self.entry.options.get("burst_cycles", DEFAULT_BURST_CYCLES)):
                selector({"number": {"min": 0, "max": 10, "step": 1}}),

            vol.Optional("low_battery_threshold", default=self.entry.options.get("low_battery_threshold", DEFAULT_LOW_BATT)):
                selector({"number": {"min": 5, "max": 50, "step": 1, "unit_of_measurement": "%"}}),

            vol.Optional("send_spacing_sec", default=self.entry.options.get("send_spacing_sec", DEFAULT_SEND_SPACING)):
                selector({"number": {"min": 0.15, "max": 1.0, "step": 0.05, "unit_of_measurement": "s"}}),

            vol.Optional("verify_enabled", default=self.entry.options.get("verify_enabled", DEFAULT_VERIFY_ENABLED)):
                selector({"boolean": {}}),

            vol.Optional("verify_delay_sec", default=self.entry.options.get("verify_delay_sec", DEFAULT_VERIFY_DELAY)):
                selector({"number": {"min": 1, "max": 60, "step": 1, "unit_of_measurement": "s"}}),
        })
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
