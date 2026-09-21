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

from typing import NotRequired, TypedDict

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
    ) -> None:
        # Ueberschreibt einen evtl. noch offenen, nie beendeten Vorgang
        # desselben Fahrzeugs (z.B. nach einer verpassten Ladeende-Meldung) -
        # gleiches Verhalten wie beim bisherigen input_text.set_value.
        self._data[vehicle_external_id] = PendingSession(
            start_time=dt_util.now().isoformat(),
            soc_start=soc_start,
            charging_type=charging_type,
            outside_temp_c=outside_temp_c,
        )
        await self._store.async_save(self._data)

    def get(self, vehicle_external_id: str) -> PendingSession | None:
        return self._data.get(vehicle_external_id)

    async def async_clear(self, vehicle_external_id: str) -> None:
        if vehicle_external_id in self._data:
            del self._data[vehicle_external_id]
            await self._store.async_save(self._data)
