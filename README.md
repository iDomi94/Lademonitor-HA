# Lademonitor – Home Assistant Integration

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

HACS-Integration für [Lademonitor-Server](https://github.com/iDomi94/Lademonitor-Server)
(selbstgehostete Ladevorgang-Tracking-App für ein E-Auto). Holt Statistiken
(Kosten, Verbrauch, gefahrene km, AC/DC-Anteil) als Sensoren in Home
Assistant und stellt Services bereit, um automatisch erkannte Ladevorgänge
an den Server zu übertragen – als Ersatz für die bisherige Kombination aus
selbst angelegten `input_text`/`input_number`-Helfern und einem
`rest_command` mit von Hand eingetragenem Bearer-Token.

## Warum diese Integration

Der Server-seitige Push-Endpunkt (`POST /api/sessions/auto`) brauchte bisher
ein Bearer-Token, das man einmalig per `curl`/`POST /api/auth/login` holen
und von Hand in eine `rest_command`-Definition eintragen musste. Diese
Integration übernimmt das: Login einmalig im Einrichtungsdialog, Token wird
automatisch verwaltet und bei Bedarf (z.B. nach einem Logout durch den
Admin) automatisch erneuert – kein manuelles Token-Handling mehr.

Zusätzlich musste man SoC-Start/Startzeit/Lade-Art bisher selbst über
`input_text`/`input_number`-Helfer zwischenspeichern (siehe unten). Die
Integration merkt sich das jetzt intern und persistent – kein Package mit
Helfer-Definitionen mehr nötig. Bewusst **keine** automatisch angelegten
`input_text`/`input_number`-Entities: eine Integration, die in die
Storage-API einer anderen Integration schreibt, wäre kein offiziell
unterstütztes Muster und würde bei HA-Updates leicht brechen.

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

## Ladevorgänge automatisch übertragen (ohne Helfer, ohne rest_command)

Bisher brauchte eine automatische Übertragung typischerweise drei Zutaten in
einer eigenen YAML-Package-Datei: `input_text`/`input_number`-Helfer (um
SoC-Start/Startzeit/Lade-Art zwischen Einstecken und Ladeende zu merken),
einen `rest_command` mit von Hand eingetragenem Bearer-Token, und die
eigentliche Automation. Die Integration übernimmt jetzt die ersten beiden
Punkte: Sie merkt sich Start-Werte **selbst persistent** (übersteht auch
einen HA-Neustart mitten im Ladevorgang) und erledigt die Auth automatisch.
Übrig bleibt nur noch die Automation selbst – die legst du wie gewohnt in
deinen eigenen Automationen an (UI oder `automations.yaml`), kein Package
mehr nötig.

Dafür gibt es zwei Services:

- **`lademonitor.begin_charging_session`** – beim Einstecken/Ladebeginn
  aufrufen, merkt `soc_start`/`charging_type` intern (Startzeit wird
  automatisch auf „jetzt" gesetzt)
- **`lademonitor.end_charging_session`** – bei Ladeende aufrufen, holt die
  gemerkten Werte wieder heraus, kombiniert sie mit den hier übergebenen
  Endwerten und überträgt den kompletten Ladevorgang an den Server

Vollständiges Beispiel für MySkoda/Škoda Enyaq (dieselbe Logik lässt sich auf
jedes Fahrzeug übertragen, das einen Lade-Status-Sensor mit einem
"eingesteckt, aber nicht ladend"-Zustand liefert). Der Škoda-Sensor
`sensor.skoda_enyaq_charging_state` kennt fünf Werte: `connect_cable`
(Ruhezustand/Standard) sowie `ready_for_charging`, `conserving`, `charging`,
`charging_interrupted` (alle vier = „verbunden/aktiv"). Die Session-Grenze
ist der Übergang zwischen `connect_cable` und einem der vier aktiven Werte –
**nicht** "Wert außerhalb einer Liste" (ein früherer, fehlerhafter Ansatz):

```yaml
automation:
  - id: enyaq_lademonitor_push
    alias: Enyaq Ladevorgang → Lademonitor
    triggers:
      - trigger: state
        entity_id: sensor.skoda_enyaq_charging_state
    actions:
      - choose:
          # connect_cable -> aktiver Zustand: Ladung beginnt
          - conditions:
              - condition: template
                value_template: >-
                  {{ trigger.from_state.state == 'connect_cable'
                     and trigger.to_state.state in
                       ['ready_for_charging','conserving','charging','charging_interrupted'] }}
            sequence:
              - action: lademonitor.begin_charging_session
                data:
                  vehicle_external_id: enyaq
                  soc_start: "{{ states('sensor.skoda_enyaq_battery_percentage') }}"
                  # Sensor liefert klein ('ac'/'dc') - Server normalisiert das selbst.
                  charging_type: "{{ states('sensor.skoda_enyaq_charge_type') }}"
          # aktiver Zustand -> connect_cable: Ladung beendet, Push an Lademonitor
          - conditions:
              - condition: template
                value_template: >-
                  {{ trigger.from_state.state in
                       ['ready_for_charging','conserving','charging','charging_interrupted']
                     and trigger.to_state.state == 'connect_cable' }}
            sequence:
              - action: lademonitor.end_charging_session
                data:
                  vehicle_external_id: enyaq
                  soc_end: "{{ states('sensor.skoda_enyaq_battery_percentage') | int }}"
                  odometer_km: "{{ states('sensor.skoda_enyaq_mileage') | int }}"
                  latitude: "{{ state_attr('device_tracker.skoda_enyaq_position', 'latitude') }}"
                  longitude: "{{ state_attr('device_tracker.skoda_enyaq_position', 'longitude') }}"
```

`energy_kwh` wird bewusst nicht mitgeschickt – der Server schätzt es
serverseitig zuverlässiger aus SoC-Delta × Akkukapazität (MySkoda liefert
keine verlässliche kWh-Angabe). Ruft man `end_charging_session` für ein
Fahrzeug auf, ohne dass vorher `begin_charging_session` lief (z.B. HA neu
gestartet, während das Auto schon lud), schlägt der Service mit einer
klaren Fehlermeldung fehl statt einen unvollständigen Datensatz zu senden.

**Migration von einer bestehenden `rest_command`-Automation mit eigenen
Helfern**: `packages/lademonitor.yaml` (die `input_text`-, `input_number`-
und `rest_command`-Definitionen) kann komplett gelöscht werden. Die
Automation selbst wandert in deine normalen Automationen und wird wie oben
umgeschrieben – Trigger und die `choose`-Struktur bleiben identisch, nur die
beiden `action`-Blöcke ändern sich (`input_number.set_value`/
`input_text.set_value` → `lademonitor.begin_charging_session`,
`rest_command.lademonitor_push_session` → `lademonitor.end_charging_session`,
kein `Authorization`-Header mehr nötig).

Für Fälle, in denen bereits alle Werte in einem Rutsch vorliegen (z.B.
Import aus einer anderen Quelle statt eines Ladebeginn/-ende-Ereignisses),
gibt es weiterhin **`lademonitor.push_charging_session`** – nimmt alle Felder
in einem Aufruf entgegen (`vehicle_external_id`, `external_session_id`,
`start_time`, `end_time`, `charging_type`, `soc_start`, `soc_end`,
`odometer_km`, `latitude`, `longitude`, `energy_kwh`).

## Bekannte Einschränkung

Fahrzeuge werden beim Einrichten der Integration einmal geladen. Ein später
im Server neu angelegtes Fahrzeug erscheint erst nach einem Neuladen der
Integration (Einstellungen → Geräte & Dienste → Lademonitor → Neu laden).
