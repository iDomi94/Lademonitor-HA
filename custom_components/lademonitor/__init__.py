"""Lademonitor Integration - Setup/Unload des ConfigEntry, Coordinator +
API-Client, und der Push-Service als Ersatz für den bisherigen
rest_command-Aufruf in der Home-Assistant-Automation des Server-Repos."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
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
    SERVICE_PUSH_CHARGING_SESSION,
)
from .coordinator import LademonitorCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

# Felder 1:1 zu schemas.AutoSessionPush (backend/app/schemas.py im Server-Repo) -
# der Server normalisiert charging_type (ac/dc -> AC/DC) und Duplikate über
# external_session_id selbst, daher hier keine zusätzliche Logik nötig.
PUSH_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_external_id"): cv.string,
        vol.Required("external_session_id"): cv.string,
        vol.Required("start_time"): cv.string,
        vol.Optional("end_time"): cv.string,
        vol.Optional("charging_type"): cv.string,
        vol.Optional("soc_start"): vol.Coerce(int),
        vol.Optional("soc_end"): vol.Coerce(int),
        vol.Optional("odometer_km"): vol.Coerce(int),
        vol.Optional("latitude"): vol.Coerce(float),
        vol.Optional("longitude"): vol.Coerce(float),
        vol.Optional("energy_kwh"): vol.Coerce(float),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = LademonitorApiClient(
        session,
        entry.data[CONF_BASE_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        token=entry.data.get(CONF_TOKEN),
    )

    try:
        vehicles = await client.async_get_vehicles()
    except LademonitorAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except LademonitorConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    # async_get_vehicles() kann intern einen Re-Login ausgelöst haben (neues
    # Token) - persistieren, damit der nächste HA-Neustart nicht unnötig
    # erneut einloggen muss.
    if client.token != entry.data.get(CONF_TOKEN):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: client.token})

    scan_minutes = entry.options.get(
        CONF_SCAN_INTERVAL_MINUTES, int(DEFAULT_SCAN_INTERVAL.total_seconds() // 60)
    )
    coordinator = LademonitorCoordinator(
        hass, client, [v["id"] for v in vehicles], timedelta(minutes=scan_minutes)
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "vehicles": vehicles,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_push_charging_session(call: ServiceCall) -> None:
        payload = {key: value for key, value in call.data.items() if value is not None}
        await client.async_push_charging_session(payload)

    # Ein Service für alle Config Entries (nur ein Lademonitor-Account im
    # typischen Ein-Haushalt-Setup vorgesehen) - bei mehreren Entries nutzt
    # der Service den Client des zuletzt eingerichteten Accounts.
    hass.services.async_register(
        DOMAIN,
        SERVICE_PUSH_CHARGING_SESSION,
        _async_push_charging_session,
        schema=PUSH_SESSION_SCHEMA,
    )

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_PUSH_CHARGING_SESSION)
    return unload_ok
