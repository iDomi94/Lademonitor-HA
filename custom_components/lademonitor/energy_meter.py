"""Gemessene Werte aus stetig steigenden Zaehlern in Home Assistant.

Ohne Zaehler schaetzt der Server die kWh eines automatisch erfassten
Ladevorgangs aus SoC-Hub x Akkukapazitaet - solche Vorgaenge zaehlen weder
beim Akku-Index noch bei den Ladeverlusten mit, weil die Schaetzung immer
genau 0 % Verlust ergaebe. Viele Wallboxen (go-e, Easee, openWB, evcc, ein
Shelly in der Zuleitung ...) haben in Home Assistant aber einen stetig
steigenden Energiezaehler. Dessen Stand wird beim Einstecken gemerkt und beim
Ladeende abgezogen.

Dasselbe Prinzip gilt fuer zwei weitere, optionale Zaehler beim Laden zu
Hause mit PV-Anlage/Hausakku (selbst gebaute Riemann-Integral-Helfer):
- Netzbezug des Autos in kWh: der Teil der Ladung, der aus dem Netz kam
  (der Rest war Sonne/Hausakku und kostet nichts),
- Kosten in Euro: das Geld fuer genau diesen Netzbezug, bereits mit dem
  jeweils gueltigen (z.B. dynamischen) Tarif gewichtet.

Bewusst GESAMTzaehler und keine "Energie dieser Ladung"-Sensoren: die setzen
sich je nach Wallbox zu unterschiedlichen Zeitpunkten zurueck (beim
Einstecken, beim Ausstecken, um Mitternacht), ein stetig steigender Zaehler
ist dagegen immer gleich auszuwerten.
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

# Obergrenze fuer die gemessenen Kosten eines Ladevorgangs: 200 kWh zu einem
# auch fuer Spitzenzeiten hohen Tarif. Darueber war es ein Zaehlersprung, und
# der Server rechnet besser mit dem Preis des Anbieters.
MAX_PLAUSIBLE_COST = 500.0


def read_counter(
    hass: HomeAssistant,
    entity_id: str | None,
    unit_factors: dict[str, float] | None = None,
) -> float | None:
    """Aktueller Zaehlerstand, oder None, wenn er nicht lesbar ist (Sensor
    unbekannt, 'unavailable', unbekannte Einheit).

    Mit unit_factors wird nach der Einheit umgerechnet (ohne Einheit gilt
    Faktor 1); ohne unit_factors wird der Wert unveraendert gelesen - etwa
    bei einem Kostenzaehler, dessen Einheit die Waehrung ist."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning("Zähler %s existiert nicht", entity_id)
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        _LOGGER.warning("Zähler %s liefert keinen Zahlenwert (%s)", entity_id, state.state)
        return None
    if unit_factors is None:
        return value
    unit = str(state.attributes.get("unit_of_measurement") or "").strip().lower()
    factor = unit_factors.get(unit, 1.0 if not unit else None)
    if factor is None:
        _LOGGER.warning("Zähler %s hat eine unbekannte Einheit (%s)", entity_id, unit)
        return None
    return value * factor


def read_energy_kwh(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Stand eines Energiezaehlers in kWh (Wh/MWh werden umgerechnet)."""
    return read_counter(hass, entity_id, _UNIT_FACTORS)


def counter_delta(
    start: float | None,
    end: float | None,
    *,
    label: str,
    digits: int,
    max_plausible: float | None = None,
    allow_zero: bool = True,
) -> float | None:
    """Differenz zweier Zaehlerstaende, oder None, wenn sie nicht bestimmbar
    oder unplausibel ist - dann wird der Wert schlicht nicht mitgeschickt,
    der Push selbst scheitert daran nie.

    Negativ heisst: der Zaehler wurde zurueckgesetzt oder getauscht. Null ist
    je nach Zaehler ein gueltiges Ergebnis (kein Netzbezug, weil die Sonne
    alles geliefert hat) oder eben nicht (an der Wallbox wurde nichts
    gezaehlt) - das steuert allow_zero."""
    if start is None or end is None:
        _LOGGER.warning(
            "%s: %s nicht lesbar, Wert wird nicht mitgeschickt",
            label,
            "Stand bei Ladebeginn" if start is None else "Stand bei Ladeende",
        )
        return None
    delta = end - start
    if delta < 0 or (max_plausible is not None and delta > max_plausible):
        _LOGGER.warning(
            "%s unplausibel (%.3f -> %.3f, Zähler zurückgesetzt?), Wert wird nicht mitgeschickt",
            label,
            start,
            end,
        )
        return None
    if delta == 0 and not allow_zero:
        _LOGGER.info("%s: Zähler unverändert, Wert wird nicht mitgeschickt", label)
        return None
    return round(delta, digits)


def measured_kwh(start_kwh: float | None, end_kwh: float | None) -> float | None:
    """Beim Laden an der Wallbox gezaehlte Energie, oder None - dann schaetzt
    der Server besser, als hier eine falsche Zahl zu schicken."""
    return counter_delta(
        start_kwh,
        end_kwh,
        label="Wallbox-kWh",
        digits=3,
        max_plausible=MAX_PLAUSIBLE_KWH,
        allow_zero=False,
    )


def measured_grid_kwh(start_kwh: float | None, end_kwh: float | None) -> float | None:
    """Waehrend des Ladens aus dem Netz bezogene Energie. 0 ist gueltig."""
    return counter_delta(
        start_kwh,
        end_kwh,
        label="Netzbezug-kWh",
        digits=3,
        max_plausible=MAX_PLAUSIBLE_KWH,
    )


def measured_cost(start: float | None, end: float | None) -> float | None:
    """Waehrend des Ladens angefallene Kosten (Waehrung des Zaehlers). 0 ist
    gueltig - reiner Sonnenstrom kostet nichts."""
    return counter_delta(
        start,
        end,
        label="Ladekosten",
        digits=2,
        max_plausible=MAX_PLAUSIBLE_COST,
    )
