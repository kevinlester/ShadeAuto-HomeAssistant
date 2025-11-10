from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_HOST, DEFAULT_POLL
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
                # Unique per host so multiple hubs are supported
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
            vol.Optional("poll_seconds", default=self.entry.options.get("poll_seconds", DEFAULT_POLL)): vol.Coerce(int),
        })
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
