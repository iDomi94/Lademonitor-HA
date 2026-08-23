# Lademonitor – Home Assistant Integration

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

HACS-Integration für [Lademonitor-Server](https://github.com/iDomi94/Lademonitor-Server)
(selbstgehostete Ladevorgang-Tracking-App für ein E-Auto). Holt Statistiken
(Kosten, Verbrauch, gefahrene km, AC/DC-Anteil) als Sensoren in Home
Assistant und stellt Services bereit, um automatisch erkannte Ladevorgänge
an den Server zu übertragen.

Login/Token-Handling läuft komplett über die Integration (Einrichtungsdialog
fragt Server-URL + Zugangsdaten einmalig ab, Token wird intern verwaltet und
bei Bedarf automatisch erneuert) – für Automationen ist kein eigenes
Auth-Handling nötig.

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

Für eine Automation, die Ladebeginn und Ladeende an zwei unterschiedlichen
Zeitpunkten erkennt (z.B. über einen Lade-Status-Sensor), gibt es zwei
Services, die zusammenspielen – SoC-Start/Startzeit/Lade-Art müssen dafür
**nirgends selbst zwischengespeichert werden** (kein `input_text`/
`input_number` nötig), die Integration merkt sich das intern und persistent
(übersteht auch einen HA-Neustart mitten im Ladevorgang):

- **`lademonitor.begin_charging_session`** – beim Einstecken/Ladebeginn
  aufrufen, merkt `soc_start`/`charging_type` intern (Startzeit wird
  automatisch auf „jetzt" gesetzt). Überträgt noch nichts an den Server.
- **`lademonitor.end_charging_session`** – bei Ladeende aufrufen, holt die
  gemerkten Werte wieder heraus, kombiniert sie mit den hier übergebenen
  Endwerten und **überträgt den kompletten Ladevorgang an den Server**
  (entspricht `POST /api/sessions/auto`).

Vollständiges Beispiel für MySkoda/Škoda Enyaq (dieselbe Logik lässt sich auf
jedes Fahrzeug übertragen, das einen Lade-Status-Sensor mit einem
"eingesteckt, aber nicht ladend"-Zustand liefert). Der Škoda-Sensor
`sensor.skoda_enyaq_charging_state` kennt fünf Werte: `connect_cable`
(Ruhezustand/Standard) sowie `ready_for_charging`, `conserving`, `charging`,
`charging_interrupted` (alle vier = „verbunden/aktiv"). Die Session-Grenze
ist der Übergang zwischen `connect_cable` und einem der vier aktiven Werte:

```yaml
automation:
  - id: enyaq_lademonitor_push
    alias: Enyaq Ladevorgang → Lademonitor
    triggers:
      - trigger: state
        entity_id: sensor.skoda_enyaq_charging_state
    actions:
      - choose:
          # connect_cable -> aktiver Zustand: Ladung beginnt, Startwerte merken
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

### Alternative: alle Werte in einem Aufruf

Liegen Start- und Endwerte bereits gemeinsam vor (z.B. eine Quelle, die den
kompletten Ladevorgang erst im Nachhinein liefert, statt Beginn und Ende als
getrennte Ereignisse), überträgt **`lademonitor.push_charging_session`** den
Ladevorgang in einem einzigen Aufruf – kein vorheriges `begin_charging_session`
nötig:

```yaml
action: lademonitor.push_charging_session
data:
  vehicle_external_id: enyaq
  external_session_id: "{{ session.id }}"
  start_time: "{{ session.start }}"
  end_time: "{{ session.end }}"
  charging_type: "{{ session.charging_type }}"
  soc_start: "{{ session.soc_start }}"
  soc_end: "{{ session.soc_end }}"
  odometer_km: "{{ session.odometer_km }}"
  latitude: "{{ session.latitude }}"
  longitude: "{{ session.longitude }}"
```

## Bekannte Einschränkung

Fahrzeuge werden beim Einrichten der Integration einmal geladen. Ein später
im Server neu angelegtes Fahrzeug erscheint erst nach einem Neuladen der
Integration (Einstellungen → Geräte & Dienste → Lademonitor → Neu laden).
