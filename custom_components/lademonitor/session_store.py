"""Persistenter Zwischenspeicher für laufende Ladevorgänge.

Ersetzt die input_text/input_number-Helfer, die man bisher selbst in einer
YAML-Package-Datei anlegen musste, um SoC-Start/Startzeit/Lade-Art zwischen
"Kabel eingesteckt" und "Ladung beendet" zwischenzuspeichern (siehe
CLAUDE.md im Server-Repo: der Charge-Type-Sensor faellt bei Ladeende oft
schon auf 'unknown' zurueck, bevor eine Automation feuert - die Werte
MUESSEN beim Einstecken gemerkt werden). Nutzt HA's eigenen Store-Helper
statt fremder input_text/input_number-Entities anzulegen (die gehoeren
einer anderen Integration - deren interne Storage-API von aussen zu nutzen
waere nicht offiziell unterstuetzt und wuerde bei HA-Updates leicht brechen).
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_VERSION = 1


class PendingSession(TypedDict):
    start_time: str
    soc_start: int | None
    charging_type: str | None
    # Aussentemperatur beim Einstecken. Aus demselben Grund gemerkt wie
    # soc_start: beim Ladeende ist sie eine andere als die, unter der die Fahrt
    # davor stattfand - und genau diese Fahrt wertet der Server spaeter als
    # Verbrauch aus. Optional, weil aeltere gespeicherte Eintraege (Storage
    # v1, vor dieser Aenderung) den Schluessel nicht haben; gelesen wird
    # deshalb ueberall mit .get().
    outside_temp_c: NotRequired[float | None]
    # Energiezaehler der Wallbox (siehe energy_meter.py): welcher Sensor und
    # sein Stand beim Einstecken, in kWh. Ebenfalls NotRequired - aeltere
    # Eintraege haben die Schluessel nicht.
    energy_sensor: NotRequired[str | None]
    energy_start_kwh: NotRequired[float | None]
    # Laden zu Hause mit PV/Hausakku (siehe energy_meter.py): Zaehler fuer den
    # Netzbezug des Autos (kWh) und fuer dessen Kosten (Euro), jeweils mit
    # Stand beim Einstecken. NotRequired wie oben.
    grid_energy_sensor: NotRequired[str | None]
    grid_energy_start_kwh: NotRequired[float | None]
    cost_sensor: NotRequired[str | None]
    cost_start: NotRequired[float | None]


class SessionStore:
    """Haelt pro vehicle_external_id maximal eine laufende Session (analog zu
    genau einem Satz Helfer-Entities pro Fahrzeug vorher)."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, PendingSession]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_pending_sessions"
        )
        self._data: dict[str, PendingSession] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    async def async_begin(
        self,
        vehicle_external_id: str,
        soc_start: int | None,
        charging_type: str | None,
        outside_temp_c: float | None = None,
        energy_sensor: str | None = None,
        energy_start_kwh: float | None = None,
        grid_energy_sensor: str | None = None,
        grid_energy_start_kwh: float | None = None,
        cost_sensor: str | None = None,
        cost_start: float | None = None,
    ) -> None:
        # Ueberschreibt einen evtl. noch offenen, nie beendeten Vorgang
        # desselben Fahrzeugs (z.B. nach einer verpassten Ladeende-Meldung) -
        # gleiches Verhalten wie beim bisherigen input_text.set_value.
        self._data[vehicle_external_id] = PendingSession(
            start_time=dt_util.now().isoformat(),
            soc_start=soc_start,
            charging_type=charging_type,
            outside_temp_c=outside_temp_c,
            energy_sensor=energy_sensor,
            energy_start_kwh=energy_start_kwh,
            grid_energy_sensor=grid_energy_sensor,
            grid_energy_start_kwh=grid_energy_start_kwh,
            cost_sensor=cost_sensor,
            cost_start=cost_start,
        )
        await self._store.async_save(self._data)

    def get(self, vehicle_external_id: str) -> PendingSession | None:
        return self._data.get(vehicle_external_id)

    async def async_clear(self, vehicle_external_id: str) -> None:
        if vehicle_external_id in self._data:
            del self._data[vehicle_external_id]
            await self._store.async_save(self._data)


class FailedPushQueue:
    """Fertige Push-Payloads, deren Uebertragung an einem voruebergehenden
    Fehler gescheitert ist (Server nicht erreichbar, HTTP 5xx ...).

    Eigener Store statt eines Feldes im SessionStore: dort haengt pro Fahrzeug
    genau EIN offener Vorgang, und der naechste begin_charging_session
    ueberschreibt ihn. Ein gescheiterter Push lag vorher genau dort und ging
    verloren, sobald vor einem erneuten Versuch wieder eingesteckt wurde.
    Erneut gesendet wird die unveraenderte Payload - Endzeit, SoC-Ende und
    Zaehlerstaende bleiben die vom Ladeende. Doppelt senden ist harmlos: der
    Server legt eine external_session_id nur einmal an."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[list[dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_failed_pushes"
        )
        self._items: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        self._items = await self._store.async_load() or []

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self._items)

    def has_vehicle(self, vehicle_external_id: str | None) -> bool:
        return any(item.get("vehicle_external_id") == vehicle_external_id for item in self._items)

    def contains(self, payload: dict[str, Any]) -> bool:
        return any(_same_session(item, payload) for item in self._items)

    async def async_add(self, payload: dict[str, Any]) -> None:
        # Derselbe Vorgang zweimal (z.B. end_charging_session von Hand
        # wiederholt) ersetzt den alten Eintrag statt ihn zu verdoppeln.
        self._items = [item for item in self._items if not _same_session(item, payload)]
        self._items.append(payload)
        await self._store.async_save(self._items)

    async def async_remove(self, payload: dict[str, Any]) -> None:
        self._items = [item for item in self._items if not _same_session(item, payload)]
        await self._store.async_save(self._items)


def _same_session(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a.get("vehicle_external_id") == b.get("vehicle_external_id")
        and a.get("external_session_id") == b.get("external_session_id")
    )
