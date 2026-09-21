"""Lademonitor Integration - Setup/Unload des ConfigEntry, Coordinator +
API-Client, und die Push-Services als Ersatz für den bisherigen
rest_command-Aufruf in der Home-Assistant-Automation des Server-Repos."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import LademonitorApiClient, LademonitorAuthError, LademonitorConnectionError
from .const import (
    CONF_BASE_URL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKEN,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_BEGIN_CHARGING_SESSION,
    SERVICE_END_CHARGING_SESSION,
    SERVICE_PUSH_CHARGING_SESSION,
)
from .coordinator import LademonitorCoordinator
from .session_store import SessionStore

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
        vol.Optional("outside_temp_c"): vol.Coerce(float),
    }
)

BEGIN_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_external_id"): cv.string,
        vol.Optional("soc_start"): vol.Coerce(int),
        vol.Optional("charging_type"): cv.string,
        vol.Optional("outside_temp_c"): vol.Coerce(float),
    }
)

END_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_external_id"): cv.string,
        vol.Optional("external_session_id"): cv.string,
        vol.Optional("soc_end"): vol.Coerce(int),
        vol.Optional("odometer_km"): vol.Coerce(int),
        vol.Optional("latitude"): vol.Coerce(float),
        vol.Optional("longitude"): vol.Coerce(float),
        vol.Optional("energy_kwh"): vol.Coerce(float),
        vol.Optional("outside_temp_c"): vol.Coerce(float),
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

    session_store = SessionStore(hass, entry.entry_id)
    await session_store.async_load()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "vehicles": vehicles,
        "session_store": session_store,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_push_charging_session(call: ServiceCall) -> None:
        """Ein einzelner Call mit allen Feldern - für Fälle, in denen bereits
        alle Werte vorliegen (z.B. andere Fahrzeug-Integration liefert schon
        einen fertigen Session-Datensatz)."""
        payload = {key: value for key, value in call.data.items() if value is not None}
        await client.async_push_charging_session(payload)

    async def _async_begin_charging_session(call: ServiceCall) -> None:
        """Beim Einstecken/Ladebeginn aufrufen - merkt SoC-Start/Startzeit/
        Lade-Art intern (siehe session_store.py), ersetzt die bisher dafür
        nötigen input_text/input_number-Helfer."""
        await session_store.async_begin(
            call.data["vehicle_external_id"],
            call.data.get("soc_start"),
            call.data.get("charging_type"),
            call.data.get("outside_temp_c"),
        )

    async def _async_end_charging_session(call: ServiceCall) -> ServiceResponse:
        """Bei Ladeende aufrufen - holt die bei begin_charging_session
        gemerkten Werte und ergänzt sie um die hier übergebenen Endwerte,
        dann derselbe Push wie push_charging_session. Gibt den kompletten
        Payload als Response zurück, damit Automationen (z.B. für eine
        Benachrichtigung) an die bei begin_charging_session gemerkten
        Startwerte kommen, ohne diese selbst zwischenspeichern zu müssen."""
        vehicle_external_id = call.data["vehicle_external_id"]
        pending = session_store.get(vehicle_external_id)
        if pending is None:
            raise HomeAssistantError(
                f"Kein offener Ladevorgang für '{vehicle_external_id}' gemerkt - "
                "wurde lademonitor.begin_charging_session beim Einstecken aufgerufen?"
            )

        payload = {
            "vehicle_external_id": vehicle_external_id,
            "external_session_id": call.data.get("external_session_id") or pending["start_time"],
            "start_time": pending["start_time"],
            "end_time": dt_util.now().isoformat(),
            "charging_type": pending["charging_type"],
            "soc_start": pending["soc_start"],
            # .get(): ein vor dieser Version gemerkter, noch offener Vorgang
            # hat den Schluessel nicht (siehe session_store.PendingSession).
            "outside_temp_c": pending.get("outside_temp_c"),
            **{
                key: value
                for key, value in call.data.items()
                if key not in ("vehicle_external_id", "external_session_id") and value is not None
            },
        }
        await client.async_push_charging_session(payload)
        await session_store.async_clear(vehicle_external_id)
        return payload

    # Services für alle Config Entries (nur ein Lademonitor-Account im
    # typischen Ein-Haushalt-Setup vorgesehen) - bei mehreren Entries nutzen
    # die Services Client/Store des zuletzt eingerichteten Accounts.
    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_CHARGING_SESSION, _async_push_charging_session, schema=PUSH_SESSION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_BEGIN_CHARGING_SESSION, _async_begin_charging_session, schema=BEGIN_SESSION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_CHARGING_SESSION,
        _async_end_charging_session,
        schema=END_SESSION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
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
            hass.services.async_remove(DOMAIN, SERVICE_BEGIN_CHARGING_SESSION)
            hass.services.async_remove(DOMAIN, SERVICE_END_CHARGING_SESSION)
    return unload_ok
