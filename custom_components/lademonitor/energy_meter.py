"""Gemessene kWh aus dem Energiezaehler der Wallbox.

Ohne Zaehler schaetzt der Server die kWh eines automatisch erfassten
Ladevorgangs aus SoC-Hub x Akkukapazitaet - solche Vorgaenge zaehlen weder
beim Akku-Index noch bei den Ladeverlusten mit, weil die Schaetzung immer
genau 0 % Verlust ergaebe. Viele Wallboxen (go-e, Easee, openWB, evcc, ein
Shelly in der Zuleitung ...) haben in Home Assistant aber einen stetig
steigenden Energiezaehler. Dessen Stand wird beim Einstecken gemerkt und beim
Ladeende abgezogen.

Bewusst ein GESAMTzaehler und kein "Energie dieser Ladung"-Sensor: der setzt
sich je nach Wallbox zu unterschiedlichen Zeitpunkten zurueck (beim
Einstecken, beim Ausstecken, um Mitternacht), ein stetig steigender Zaehler
ist dagegen bei jeder Wallbox gleich auszuwerten.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Umrechnung nach kWh je unit_of_measurement. Ein Sensor ohne Einheit wird als
# kWh gelesen - das ist bei Energiezaehlern in HA der Normalfall.
_UNIT_FACTORS = {
    "wh": 0.001,
    "kwh": 1.0,
    "mwh": 1000.0,
}

# Mehr passt in keinen heute erhaeltlichen Pkw-Akku samt Verlusten. Alles
# darueber ist ein Zaehlerfehler (z.B. Sprung nach einem Firmware-Update) und
# wuerde Kosten und Verbrauch kraeftig verzerren - dann lieber schaetzen.
MAX_PLAUSIBLE_KWH = 200.0


def read_energy_kwh(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Aktueller Zaehlerstand in kWh, oder None, wenn er nicht lesbar ist
    (Sensor unbekannt, 'unavailable', unbekannte Einheit)."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning("Energiezähler %s existiert nicht", entity_id)
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        _LOGGER.info("Energiezähler %s liefert keinen Zahlenwert (%s)", entity_id, state.state)
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "kWh").strip().lower()
    factor = _UNIT_FACTORS.get(unit)
    if factor is None:
        _LOGGER.warning("Energiezähler %s hat eine unbekannte Einheit (%s)", entity_id, unit)
        return None
    return value * factor


def measured_kwh(start_kwh: float | None, end_kwh: float | None) -> float | None:
    """Beim Laden gezaehlte Energie, oder None, wenn sie unplausibel ist.

    Null oder negativ heisst: der Zaehler wurde zurueckgesetzt oder getauscht,
    oder es wurde schlicht nicht geladen. In allen Faellen schaetzt der Server
    besser, als hier eine falsche Zahl zu schicken."""
    if start_kwh is None or end_kwh is None:
        return None
    delta = end_kwh - start_kwh
    if delta <= 0 or delta > MAX_PLAUSIBLE_KWH:
        _LOGGER.info(
            "Wallbox-kWh unplausibel (%.3f -> %.3f), der Server schätzt stattdessen",
            start_kwh,
            end_kwh,
        )
        return None
    return round(delta, 3)
