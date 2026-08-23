# Lademonitor – Home Assistant Integration

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

HACS-Integration für [Lademonitor-Server](https://github.com/iDomi94/Lademonitor-Server)
(selbstgehostete Ladevorgang-Tracking-App für ein E-Auto). Holt Statistiken
(Kosten, Verbrauch, gefahrene km, AC/DC-Anteil) als Sensoren in Home
Assistant und stellt einen Service bereit, um automatisch erkannte
Ladevorgänge an den Server zu übertragen – als Ersatz für den bisherigen,
manuell konfigurierten `rest_command`-Aufruf.

## Warum diese Integration

Der Server-seitige Push-Endpunkt (`POST /api/sessions/auto`) brauchte bisher
ein Bearer-Token, das man einmalig per `curl`/`POST /api/auth/login` holen
und von Hand in eine `rest_command`-Definition eintragen musste. Diese
Integration übernimmt das: Login einmalig im Einrichtungsdialog, Token wird
automatisch verwaltet und bei Bedarf (z.B. nach einem Logout durch den
Admin) automatisch erneuert – kein manuelles Token-Handling mehr.

## Installation (HACS Custom Repository)

Solange die Integration nicht im HACS-Standard-Store gelistet ist:

1. HACS → Integrationen → Menü (⋮) → *Benutzerdefinierte Repositories*
2. URL dieses Repos eintragen, Kategorie *Integration*
3. „Lademonitor" installieren, Home Assistant neu starten
4. **Einstellungen → Geräte & Dienste → Integration hinzufügen → Lademonitor**
5. Server-URL, Benutzername und Passwort deines Lademonitor-Accounts eingeben
   (der Account, dem das Fahrzeug gehört – siehe Hinweis zur
   Pro-Nutzer-Datentrennung in `Lademonitor-Server/CLAUDE.md`)

## Sensoren

Pro Fahrzeug (aus `GET /api/vehicles`) ein Device mit: Gesamtkosten,
Gesamt-kWh, Ø Preis/kWh, Ø Verbrauch kWh/100km, Kosten/100km, gefahrene km
gesamt, AC-/DC-Anteil, Anzahl Ladevorgänge. Abfrageintervall über die
Integrations-Optionen einstellbar (Standard 15 Minuten).

## Ladevorgänge automatisch übertragen

Ersetzt den bisherigen `rest_command` in `packages/lademonitor.yaml`:

```yaml
# vorher:
# action: rest_command.lademonitor_push_session

# nachher:
action: lademonitor.push_charging_session
data:
  vehicle_external_id: enyaq
  external_session_id: "{{ now().strftime('%Y%m%d_%H%M') }}"
  start_time: "{{ states('input_text.enyaq_charge_start') }}"
  end_time: "{{ now().isoformat() }}"
  charging_type: "{{ states('input_text.enyaq_charge_type') }}"
  soc_start: "{{ states('input_number.enyaq_soc_start') | int }}"
  soc_end: "{{ states('sensor.skoda_enyaq_battery_percentage') | int }}"
  odometer_km: "{{ states('sensor.skoda_enyaq_mileage') | int }}"
  latitude: "{{ state_attr('device_tracker.skoda_enyaq_position', 'latitude') }}"
  longitude: "{{ state_attr('device_tracker.skoda_enyaq_position', 'longitude') }}"
```

Die restliche Automation (Trigger auf `sensor.skoda_enyaq_charging_state`,
`input_text`/`input_number`-Helper zum Zwischenspeichern von SoC-Start/
Startzeit/Lade-Art) bleibt unverändert – nur der HTTP-Call ändert sich.

## Bekannte Einschränkung

Fahrzeuge werden beim Einrichten der Integration einmal geladen. Ein später
im Server neu angelegtes Fahrzeug erscheint erst nach einem Neuladen der
Integration (Einstellungen → Geräte & Dienste → Lademonitor → Neu laden).
