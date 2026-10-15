"""Lademonitor Integration - Setup/Unload des ConfigEntry, Coordinator +
API-Client, und die Push-Services als Ersatz für den bisherigen
rest_command-Aufruf in der Home-Assistant-Automation des Server-Repos."""

from __future__ import annotations

import logging
import math
from datetime import timedelta

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from voluptuous.humanize import humanize_error

from .api import (
    LademonitorApiClient,
    LademonitorApiError,
    LademonitorAuthError,
    LademonitorConnectionError,
)
from .const import (
    CONF_BASE_URL,
    EVENT_PUSH_FAILED,
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
from .energy_meter import (
    measured_cost,
    measured_grid_kwh,
    measured_kwh,
    read_counter,
    read_energy_kwh,
)
from .session_store import SessionStore

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


def _optional_entity_id(value: object) -> str | None:
    """Optionaler Sensor, der einen Ladevorgang nie zum Scheitern bringen darf.

    Leer, None oder etwas, das keine Entitaets-ID ist, heisst schlicht "kein
    Sensor" - dann fehlt der Wert eben (der Server schaetzt z.B. die kWh wie
    ohne Zaehler bzw. nimmt den Preis des Anbieters). Vorher lehnte
    der Service ein leeres Feld ab, und mit ihm ging der ganze Push verloren."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return cv.entity_id(value)
    except vol.Invalid:
        _LOGGER.warning("Zähler %r ist keine gültige Entität - wird ignoriert", value)
        return None


def _optional_non_negative(value: object) -> float | None:
    """Gemessener Netzbezug (kWh) bzw. gemessene Kosten eines Ladevorgangs.

    Beide >= 0, 0 ist gueltig (reiner Sonnenstrom). Wie bei den optionalen
    Sensoren gilt: ein leerer, negativer oder unlesbarer Wert faellt weg,
    statt den ganzen Push scheitern zu lassen."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        _LOGGER.warning("Wert %r ist keine Zahl - wird ignoriert", value)
        return None
    if not math.isfinite(number) or number < 0:
        _LOGGER.warning("Wert %r ist negativ oder ungültig - wird ignoriert", value)
        return None
    return number


# Felder 1:1 zu schemas.AutoSessionPush (backend/app/schemas.py im Server-Repo) -
# der Server normalisiert charging_type (ac/dc -> AC/DC) und Duplikate über
# external_session_id selbst, daher hier keine zusätzliche Logik nötig.
PUSH_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_external_id"): cv.string,
        vol.Required("external_session_id"): cv.string,
        vol.Required("start_time"): cv.string,
        vol.Optional("end_time"): vol.Any(None, cv.string),
        vol.Optional("charging_type"): vol.Any(None, cv.string),
        vol.Optional("soc_start"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("soc_end"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("odometer_km"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("latitude"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("longitude"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("energy_kwh"): vol.Any(None, vol.Coerce(float)),
        # Wo energy_kwh gemessen wurde: "charger" (Wallbox/Ladesaeule) oder
        # "vehicle". Ohne Angabe gilt die Einstellung des Anbieters im Server.
        vol.Optional("energy_meter"): vol.Any(None, vol.In(["charger", "vehicle"])),
        vol.Optional("outside_temp_c"): vol.Any(None, vol.Coerce(float)),
        # Laden zu Hause mit PV/Hausakku: davon aus dem Netz bezogene kWh und
        # die gemessenen Gesamtkosten (ersetzen im Server den Anbieterpreis).
        # Server vor 0.41.0 ignorieren beide Felder.
        vol.Optional("grid_energy_kwh"): _optional_non_negative,
        vol.Optional("price_total"): _optional_non_negative,
    },
    extra=vol.REMOVE_EXTRA,
)

BEGIN_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_external_id"): cv.string,
        vol.Optional("soc_start"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("charging_type"): vol.Any(None, cv.string),
        vol.Optional("outside_temp_c"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("energy_sensor"): _optional_entity_id,
        vol.Optional("grid_energy_sensor"): _optional_entity_id,
        vol.Optional("cost_sensor"): _optional_entity_id,
    },
    extra=vol.REMOVE_EXTRA,
)

END_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_external_id"): cv.string,
        vol.Optional("external_session_id"): vol.Any(None, cv.string),
        vol.Optional("soc_end"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("odometer_km"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("latitude"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("longitude"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("energy_kwh"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("outside_temp_c"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("energy_sensor"): _optional_entity_id,
        vol.Optional("grid_energy_sensor"): _optional_entity_id,
        vol.Optional("cost_sensor"): _optional_entity_id,
        # Feste Werte statt Zaehler - haben Vorrang vor den Sensoren.
        vol.Optional("grid_energy_kwh"): _optional_non_negative,
        vol.Optional("price_total"): _optional_non_negative,
    },
    extra=vol.REMOVE_EXTRA,
)

# Beim Registrieren nur das Noetigste pruefen; die eigentliche Pruefung laeuft
# im Service selbst (_validate). Ein Schema-Fehler beim Registrieren bricht die
# Automation ab, BEVOR die Integration etwas davon mitbekommt - es gaebe dann
# keine Benachrichtigung, und "continue_on_error" greift bei Schema-Fehlern
# auch nicht. Unbekannte Felder (z.B. ein neuerer Blueprint gegen eine aeltere
# Integration) werden verworfen statt den Push scheitern zu lassen.
LENIENT_SCHEMA = vol.Schema({vol.Required("vehicle_external_id"): cv.string}, extra=vol.ALLOW_EXTRA)

# Felder von end_charging_session, die nicht 1:1 in den Push gehen (die
# Zaehler-Sensoren werden zu Differenzen verrechnet).
_END_ONLY_FIELDS = (
    "vehicle_external_id",
    "external_session_id",
    "energy_sensor",
    "grid_energy_sensor",
    "cost_sensor",
)

NOTIFICATION_TITLE = "Lademonitor: Ladevorgang nicht übertragen"


def _notification_id(vehicle_external_id: str | None) -> str:
    return f"lademonitor_push_failed_{slugify(vehicle_external_id or 'unbekannt')}"


def _report_failure(
    hass: HomeAssistant,
    service: str,
    vehicle_external_id: str | None,
    error: str,
    session: dict | None = None,
) -> None:
    """Meldet einen gescheiterten Push sichtbar statt nur im Log.

    Zwei Wege: eine dauerhafte Benachrichtigung in Home Assistant (ohne jede
    Einrichtung, bleibt bis zum Wegklicken bzw. bis zum naechsten
    erfolgreichen Push dieses Fahrzeugs stehen) und das Ereignis
    `lademonitor_push_failed`, auf das eigene Automationen reagieren koennen
    (z.B. Push aufs Handy). Die Werte des Ladevorgangs stehen mit drin, damit
    er sich von Hand nachtragen laesst."""
    _LOGGER.error("lademonitor.%s fehlgeschlagen (%s): %s", service, vehicle_external_id, error)
    lines = [
        f"**Fahrzeug:** {vehicle_external_id or '–'}",
        f"**Fehler:** {error}",
    ]
    if session:
        labels = (
            ("start_time", "Start"),
            ("end_time", "Ende"),
            ("soc_start", "SoC Start (%)"),
            ("soc_end", "SoC Ende (%)"),
            ("charging_type", "Lade-Art"),
            ("energy_kwh", "Energie (kWh)"),
            ("grid_energy_kwh", "davon aus dem Netz (kWh)"),
            ("price_total", "Kosten gesamt"),
            ("odometer_km", "Kilometerstand"),
            ("outside_temp_c", "Außentemperatur (°C)"),
        )
        values = [f"- {label}: {session[key]}" for key, label in labels if session.get(key) is not None]
        if values:
            lines += ["", "Werte zum Nachtragen im Lademonitor:", *values]
    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title=NOTIFICATION_TITLE,
        notification_id=_notification_id(vehicle_external_id),
    )
    hass.bus.async_fire(
        EVENT_PUSH_FAILED,
        {
            "service": service,
            "vehicle_external_id": vehicle_external_id,
            "error": error,
            "session": session,
        },
    )


def _validate(hass: HomeAssistant, schema: vol.Schema, call: ServiceCall) -> dict:
    data = dict(call.data)
    try:
        validated = schema(data)
    except vol.Invalid as err:
        error = f"Ungültige Angabe: {humanize_error(data, err)}"
        _report_failure(hass, call.service, data.get("vehicle_external_id"), error)
        raise ServiceValidationError(error) from err
    if dropped := sorted(set(data) - set(validated)):
        _LOGGER.warning("lademonitor.%s: unbekannte Felder ignoriert: %s", call.service, ", ".join(dropped))
    return validated


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
        data = _validate(hass, PUSH_SESSION_SCHEMA, call)
        payload = {key: value for key, value in data.items() if value is not None}
        await _async_push(call.service, payload)

    async def _async_push(service: str, payload: dict) -> None:
        """Push mit Benachrichtigung im Fehlerfall. Wirft HomeAssistantError,
        damit eine Automation mit `continue_on_error: true` weiterlaufen und
        selbst reagieren kann (siehe Blueprint)."""
        vehicle_external_id = payload.get("vehicle_external_id")
        try:
            await client.async_push_charging_session(payload)
        except LademonitorAuthError as err:
            error = f"Anmeldung am Lademonitor-Server fehlgeschlagen: {err}"
        except LademonitorApiError as err:
            error = f"Lademonitor-Server hat den Ladevorgang abgelehnt: {err}"
        except LademonitorConnectionError as err:
            error = f"Lademonitor-Server nicht erreichbar: {err}"
        except Exception as err:  # noqa: BLE001 - jeder Fehler soll gemeldet werden
            _LOGGER.exception("Unerwarteter Fehler beim Push")
            error = f"Unerwarteter Fehler: {err}"
        else:
            persistent_notification.async_dismiss(hass, _notification_id(vehicle_external_id))
            return
        _report_failure(hass, service, vehicle_external_id, error, payload)
        raise HomeAssistantError(error)

    async def _async_begin_charging_session(call: ServiceCall) -> None:
        """Beim Einstecken/Ladebeginn aufrufen - merkt SoC-Start/Startzeit/
        Lade-Art intern (siehe session_store.py), ersetzt die bisher dafür
        nötigen input_text/input_number-Helfer."""
        data = _validate(hass, BEGIN_SESSION_SCHEMA, call)
        energy_sensor = data.get("energy_sensor")
        grid_energy_sensor = data.get("grid_energy_sensor")
        cost_sensor = data.get("cost_sensor")
        await session_store.async_begin(
            data["vehicle_external_id"],
            data.get("soc_start"),
            data.get("charging_type"),
            data.get("outside_temp_c"),
            energy_sensor=energy_sensor,
            energy_start_kwh=read_energy_kwh(hass, energy_sensor),
            grid_energy_sensor=grid_energy_sensor,
            grid_energy_start_kwh=read_energy_kwh(hass, grid_energy_sensor),
            cost_sensor=cost_sensor,
            cost_start=read_counter(hass, cost_sensor),
        )

    async def _async_end_charging_session(call: ServiceCall) -> ServiceResponse:
        """Bei Ladeende aufrufen - holt die bei begin_charging_session
        gemerkten Werte und ergänzt sie um die hier übergebenen Endwerte,
        dann derselbe Push wie push_charging_session. Gibt den kompletten
        Payload als Response zurück, damit Automationen (z.B. für eine
        Benachrichtigung) an die bei begin_charging_session gemerkten
        Startwerte kommen, ohne diese selbst zwischenspeichern zu müssen."""
        data = _validate(hass, END_SESSION_SCHEMA, call)
        vehicle_external_id = data["vehicle_external_id"]
        pending = session_store.get(vehicle_external_id)
        if pending is None:
            error = (
                f"Kein offener Ladevorgang für '{vehicle_external_id}' gemerkt - "
                "wurde lademonitor.begin_charging_session beim Einstecken aufgerufen?"
            )
            _report_failure(
                hass,
                call.service,
                vehicle_external_id,
                error,
                {key: value for key, value in data.items() if value is not None},
            )
            raise HomeAssistantError(error)

        payload = {
            "vehicle_external_id": vehicle_external_id,
            "external_session_id": data.get("external_session_id") or pending["start_time"],
            "start_time": pending["start_time"],
            "end_time": dt_util.now().isoformat(),
            "charging_type": pending["charging_type"],
            "soc_start": pending["soc_start"],
            # .get(): ein vor dieser Version gemerkter, noch offener Vorgang
            # hat den Schluessel nicht (siehe session_store.PendingSession).
            "outside_temp_c": pending.get("outside_temp_c"),
            **{
                key: value
                for key, value in data.items()
                if key not in _END_ONLY_FIELDS and value is not None
            },
        }
        # Wallbox-Zaehler: nur, wenn nicht ohnehin eine kWh-Angabe uebergeben
        # wurde - die hat Vorrang. Der Sensor beim Ladeende darf fehlen, dann
        # gilt der beim Einstecken gemerkte.
        energy_sensor = data.get("energy_sensor") or pending.get("energy_sensor")
        if data.get("energy_kwh") is None and energy_sensor:
            kwh = measured_kwh(pending.get("energy_start_kwh"), read_energy_kwh(hass, energy_sensor))
            if kwh is not None:
                payload["energy_kwh"] = kwh
                # Gemessen an der Wallbox, also an der Ladesaeule - auch wenn
                # der Heim-Anbieter im Server auf "Fahrzeug" steht.
                payload["energy_meter"] = "charger"
        # Netzbezug und Kosten (PV/Hausakku): gleiches Prinzip, ein direkt
        # uebergebener Wert hat Vorrang. Ist ein Zaehler nicht lesbar oder
        # zurueckgesetzt, fehlt nur dieser Wert - der Push laeuft trotzdem.
        grid_energy_sensor = data.get("grid_energy_sensor") or pending.get("grid_energy_sensor")
        if data.get("grid_energy_kwh") is None and grid_energy_sensor:
            grid_kwh = measured_grid_kwh(
                pending.get("grid_energy_start_kwh"), read_energy_kwh(hass, grid_energy_sensor)
            )
            if grid_kwh is not None:
                payload["grid_energy_kwh"] = grid_kwh
        cost_sensor = data.get("cost_sensor") or pending.get("cost_sensor")
        if data.get("price_total") is None and cost_sensor:
            cost = measured_cost(pending.get("cost_start"), read_counter(hass, cost_sensor))
            if cost is not None:
                payload["price_total"] = cost
        # Schlaegt der Push fehl, bleibt der gemerkte Vorgang stehen (kein
        # async_clear) - ein erneuter Aufruf von end_charging_session
        # uebertraegt ihn nachtraeglich.
        await _async_push(call.service, payload)
        await session_store.async_clear(vehicle_external_id)
        return payload

    # Services für alle Config Entries (nur ein Lademonitor-Account im
    # typischen Ein-Haushalt-Setup vorgesehen) - bei mehreren Entries nutzen
    # die Services Client/Store des zuletzt eingerichteten Accounts.
    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_CHARGING_SESSION, _async_push_charging_session, schema=LENIENT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_BEGIN_CHARGING_SESSION, _async_begin_charging_session, schema=LENIENT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_CHARGING_SESSION,
        _async_end_charging_session,
        schema=LENIENT_SCHEMA,
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
