"""Config Flow für Lademonitor - fragt einmalig Server-URL + Zugangsdaten ab
und validiert per echtem Login-Call. Ersetzt das bisherige manuelle
'POST /api/auth/login per curl, Token in YAML eintragen' aus der
Home-Assistant-Anbindung des Server-Repos."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LademonitorApiClient, LademonitorAuthError, LademonitorConnectionError
from .const import (
    CONF_BASE_URL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKEN,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_login(hass: HomeAssistant, base_url: str, username: str, password: str) -> str:
    """Loggt sich testweise ein, gibt bei Erfolg das Token zurück. Wirft
    LademonitorAuthError/LademonitorConnectionError bei Fehlschlag."""
    session = async_get_clientsession(hass)
    client = LademonitorApiClient(session, base_url, username, password)
    return await client.async_login()


class LademonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            username = user_input[CONF_USERNAME]
            try:
                token = await _validate_login(self.hass, base_url, username, user_input[CONF_PASSWORD])
            except LademonitorAuthError:
                errors["base"] = "invalid_auth"
            except LademonitorConnectionError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{base_url}::{username}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Lademonitor ({username})",
                    data={
                        CONF_BASE_URL: base_url,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_TOKEN: token,
                    },
                )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._reauth_entry is not None
        if user_input is not None:
            try:
                token = await _validate_login(
                    self.hass,
                    self._reauth_entry.data[CONF_BASE_URL],
                    self._reauth_entry.data[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except LademonitorAuthError:
                errors["base"] = "invalid_auth"
            except LademonitorConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_TOKEN: token,
                    },
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> LademonitorOptionsFlow:
        return LademonitorOptionsFlow(config_entry)


class LademonitorOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL_MINUTES, int(DEFAULT_SCAN_INTERVAL.total_seconds() // 60)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_SCAN_INTERVAL_MINUTES, default=current): int}),
        )
