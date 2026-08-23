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

## Beispiel-Automation: Ladevorgänge automatisch übertragen

Vollständiges Beispiel für MySkoda/Škoda Enyaq (dieselbe Logik lässt sich auf
jedes Fahrzeug übertragen, das einen Lade-Status-Sensor mit einem
"eingesteckt, aber nicht ladend"-Zustand liefert). Der Škoda-Sensor
`sensor.skoda_enyaq_charging_state` kennt fünf Werte: `connect_cable`
(Ruhezustand/Standard) sowie `ready_for_charging`, `conserving`, `charging`,
`charging_interrupted` (alle vier = „verbunden/aktiv"). Die Session-Grenze
ist der Übergang zwischen `connect_cable` und einem der vier aktiven Werte –
**nicht** "Wert außerhalb einer Liste" (ein früherer, fehlerhafter Ansatz).

SoC-Start, Startzeit und Lade-Art müssen beim **Einstecken** zwischengespeichert
werden, weil der Charge-Type-Sensor beim Ladeende oft schon auf `unknown`
zurückfällt, bevor die Automation feuert (Timing-Problem, nicht vermeidbar).

Als HA-Package ablegen (z.B. `packages/lademonitor.yaml`, damit
`configuration.yaml` sauber bleibt):

```yaml
input_text:
  enyaq_charge_start:
    name: Lademonitor - Ladebeginn (intern)
  enyaq_charge_type:
    name: Lademonitor - Lade-Art (intern)

input_number:
  enyaq_soc_start:
    name: Lademonitor - SoC bei Ladebeginn (intern)
    min: 0
    max: 100

automation:
  - id: lademonitor_ladebeginn_merken
    alias: "Lademonitor: Ladebeginn merken"
    trigger:
      - platform: state
        entity_id: sensor.skoda_enyaq_charging_state
        from: "connect_cable"
        to:
          - "ready_for_charging"
          - "conserving"
          - "charging"
          - "charging_interrupted"
    action:
      - service: input_number.set_value
        target:
          entity_id: input_number.enyaq_soc_start
        data:
          value: "{{ states('sensor.skoda_enyaq_battery_percentage') }}"
      - service: input_text.set_value
        target:
          entity_id: input_text.enyaq_charge_start
        data:
          value: "{{ now().isoformat() }}"
      - service: input_text.set_value
        target:
          entity_id: input_text.enyaq_charge_type
        data:
          # Sensor liefert klein ('ac'/'dc') - Server normalisiert das
          # selbst, hier keine Umwandlung nötig.
          value: "{{ states('sensor.skoda_enyaq_charge_type') }}"

  - id: lademonitor_ladeende_uebertragen
    alias: "Lademonitor: Ladeende an Server übertragen"
    trigger:
      - platform: state
        entity_id: sensor.skoda_enyaq_charging_state
        to: "connect_cable"
        from:
          - "ready_for_charging"
          - "conserving"
          - "charging"
          - "charging_interrupted"
    condition:
      # Ohne gemerkte Startzeit (z.B. HA-Neustart mitten im Ladevorgang)
      # lieber gar nichts übertragen, statt einen unsinnigen Push zu senden.
      - condition: template
        value_template: >-
          {{ states('input_text.enyaq_charge_start') not in
             ['unknown', 'unavailable', ''] }}
    action:
      - action: lademonitor.push_charging_session
        data:
          vehicle_external_id: enyaq
          external_session_id: "{{ states('input_text.enyaq_charge_start') }}"
          start_time: "{{ states('input_text.enyaq_charge_start') }}"
          end_time: "{{ now().isoformat() }}"
          charging_type: "{{ states('input_text.enyaq_charge_type') }}"
          soc_start: "{{ states('input_number.enyaq_soc_start') | int }}"
          soc_end: "{{ states('sensor.skoda_enyaq_battery_percentage') | int }}"
          odometer_km: "{{ states('sensor.skoda_enyaq_mileage') | int }}"
          latitude: >-
            {{ state_attr('device_tracker.skoda_enyaq_position', 'latitude') }}
          longitude: >-
            {{ state_attr('device_tracker.skoda_enyaq_position', 'longitude') }}
```

`external_session_id` nutzt hier direkt die gemerkte Startzeit als
Duplikatschutz (eindeutig pro Ladevorgang, kein Zeitzonen-Rundungsproblem wie
bei einem separat neu berechneten Zeitstempel). `energy_kwh` wird bewusst
nicht mitgeschickt – der Server schätzt es serverseitig zuverlässiger aus
SoC-Delta × Akkukapazität (MySkoda liefert keine verlässliche kWh-Angabe).

**Migration von einer bestehenden `rest_command`-Automation**: Trigger,
Condition und die `input_text`/`input_number`-Helfer bleiben identisch – nur
der `action`-Block der zweiten Automation wird ersetzt (`service:
rest_command.lademonitor_push_session` → `action:
lademonitor.push_charging_session` mit denselben Daten-Feldern, kein
`Authorization`-Header mehr nötig).

## Bekannte Einschränkung

Fahrzeuge werden beim Einrichten der Integration einmal geladen. Ein später
im Server neu angelegtes Fahrzeug erscheint erst nach einem Neuladen der
Integration (Einstellungen → Geräte & Dienste → Lademonitor → Neu laden).
