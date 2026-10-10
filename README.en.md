# Lademonitor – Home Assistant Integration

**Language:** English | [Deutsch](README.md)

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

HACS integration for [Lademonitor-Server](https://github.com/iDomi94/Lademonitor-Server)
(a self-hosted charging session tracking app for an EV). Pulls statistics
(cost, consumption, km driven, AC/DC share) into Home Assistant as sensors
and provides services to push automatically detected charging sessions to
the server.

Login/token handling runs entirely through the integration (the setup
dialog asks for the server URL + credentials once, the token is managed
internally and renewed automatically as needed) – no separate auth
handling is required for automations.

## Installation (HACS Custom Repository)

As long as the integration is not listed in the standard HACS store:

1. HACS → Integrations → Menu (⋮) → *Custom repositories*
2. Enter this repo's URL, category *Integration*
3. Install "Lademonitor", restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Lademonitor**
5. Enter the server URL, username, and password of your Lademonitor account
   (the account that owns the vehicle – see the note on per-user data
   separation in `Lademonitor-Server/CLAUDE.md`)

## Sensors

One device per vehicle (from `GET /api/vehicles`) with: total cost, total
kWh, average price/kWh, average consumption kWh/100km, cost/100km, total km
driven, AC/DC share, number of charging sessions. The polling interval is
configurable via the integration's options (default 15 minutes).

Some sensors also carry the monthly/provider breakdown from
`GET /api/stats/summary` as an attribute, instead of having their own
dedicated sensors for it:

| Sensor | Attribute(s) |
| --- | --- |
| Total cost | `monthly` (month + cost), `by_provider` (provider + cost) |
| Total energy charged | `monthly` (month + kWh), `by_provider` (provider + kWh) |
| Average consumption | `monthly` (month + average consumption) |
| Charging sessions | `monthly` (month + number of charging sessions) |
| AC share / DC share | `ac_kwh` / `dc_kwh` (absolute value instead of just percentage) |

Usable e.g. with `custom:apexcharts-card` (HACS) for provider pie or
monthly bar charts like in the app/web UI – with native Lovelace cards,
list-type attributes cannot be rendered directly as a chart.

## Automatically pushing charging sessions

For an automation that detects the start and end of a charging session at
two different points in time (e.g. via a charging-status sensor), there
are two services that work together – for this, the start SoC/start
time/charging type **don't need to be cached anywhere yourself**
(no `input_text`/`input_number` needed), the integration remembers this
internally and persistently (survives an HA restart in the middle of a
charging session as well):

- **`lademonitor.begin_charging_session`** – call when plugging in/starting
  to charge, remembers `soc_start`/`charging_type`/`outside_temp_c`
  internally (the start time is automatically set to "now"). Does not push
  anything to the server yet.
- **`lademonitor.end_charging_session`** – call when charging ends,
  retrieves the remembered values, combines them with the end values
  passed here, and **pushes the complete charging session to the server**
  (equivalent to `POST /api/sessions/auto`).

### Outside temperature (for the consumption analysis)

From Lademonitor-Server 0.23.0 the dashboard analyses how consumption relates
to the outside temperature - so "what does winter cost me" can be answered for
your own car instead of with rules of thumb from the internet.

The server needs one value per charging session for that: `outside_temp_c`, in
degrees Celsius, **at the start of charging**. The timing is not a detail but
the whole point: the consumption the server attributes to a charging session
comes from the drive **before** it - and that drive ends the moment you plug
in. A temperature measured when charging ends would, after hours at the
wallbox, be a different one.

So `begin_charging_session` remembers the value internally, exactly like the
starting SoC and the charging type, and `end_charging_session` sends it along.
The blueprint has an optional **"Outside temperature sensor"** input for this;
by hand:

```yaml
action: lademonitor.begin_charging_session
data:
  vehicle_external_id: enyaq
  soc_start: "{{ states('sensor.skoda_enyaq_battery_percentage') }}"
  charging_type: "{{ states('sensor.skoda_enyaq_charge_type') }}"
  # float(default=None) catches 'unknown'/'unavailable' - otherwise the server
  # rejects the value (only the value; the session itself still arrives).
  outside_temp_c: "{{ states('sensor.skoda_enyaq_outside_temperature') | float(default=None) }}"
```

Which sensor is up to you: the car's own outside temperature sensor is the
obvious one, a weather integration or a thermometer at the parking spot work
just as well. For a `weather.` entity the value lives in an attribute:

```yaml
  outside_temp_c: "{{ state_attr('weather.home', 'temperature') | float(default=None) }}"
```

Without a temperature everything works as before - the analysis simply stays
empty and reports how many sessions it is missing.

### Wallbox meter (measured kWh)

Without a meter, the server estimates the kWh of an automatically recorded
session from the SoC delta × battery capacity. That is fine for cost and
consumption, but such sessions are left out of the **battery index and
charging losses**: an estimate would always show exactly 0 % loss.

If your wallbox has an energy meter in Home Assistant (go-e, Easee, openWB,
evcc, a Shelly in the supply line …), the integration can read it from
version 0.6.0: it remembers the reading when charging starts and sends the
difference as measured kWh when it ends. The blueprint has the optional input
**"Energiezähler der Wallbox"** for this; by hand:

```yaml
action: lademonitor.begin_charging_session
data:
  vehicle_external_id: enyaq
  soc_start: "{{ states('sensor.skoda_enyaq_battery_percentage') }}"
  energy_sensor: sensor.wallbox_energy_total
```

Use the **total** meter that keeps counting up, not an "energy of this
session" sensor. Wh and MWh are converted automatically. An implausible
difference (0, negative after a meter reset, or above 200 kWh) is dropped and
the server estimates as before. The kWh count as measured at the charger, even
if your home provider is set to "vehicle" in Lademonitor. Requires
Lademonitor server 0.31.0.

### Blueprint (recommended)

[![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FiDomi94%2FLademonitor-HA%2Fmain%2Fblueprints%2Fautomation%2Flademonitor%2Fcharging_session.yaml)

Covers `begin_charging_session`/`end_charging_session`, including an
optional mobile app notification when charging ends ("Charging session
sent to Lademonitor: start … → end …, SoC …% → …%, AC/DC"). Just import via
the button and in the form select the charging-status sensor,
battery-level sensor, as well as (optionally) the charging-type/odometer/
location sensor and the notify entity for the notification – idle/active
states are pre-filled with the Enyaq/MySkoda values, but adjustable for
other vehicles. Source:
[`blueprints/automation/lademonitor/charging_session.yaml`](blueprints/automation/lademonitor/charging_session.yaml).

The import button simply links to a raw YAML file – no Gist is needed for
that, a raw GitHub URL from a public repo (like this one) works just as
well.

The notification uses the service response newly introduced with
`end_charging_session`: the service returns the complete pushed charging
session (including the start values remembered at
`begin_charging_session`), retrievable via `response_variable` – for your
own automations, for example like this:

```yaml
- action: lademonitor.end_charging_session
  data:
    vehicle_external_id: enyaq
    soc_end: "{{ states('sensor.skoda_enyaq_battery_percentage') | int }}"
  response_variable: lademonitor_session
- action: notify.send_message
  target:
    entity_id: notify.mein_handy
  data:
    message: >-
      Ladevorgang zu Lademonitor gesendet: Start
      {{ lademonitor_session.start_time }} → Ende {{ lademonitor_session.end_time }},
      SoC {{ lademonitor_session.soc_start }}% → {{ lademonitor_session.soc_end }}%,
      {{ lademonitor_session.charging_type }}
```

### Manual (without blueprint)

Complete example for MySkoda/Škoda Enyaq (the same logic can be applied to
any vehicle that provides a charging-status sensor with a "plugged in but
not charging" state). The Škoda sensor
`sensor.skoda_enyaq_charging_state` knows five values: `connect_cable`
(idle state/default) as well as `ready_for_charging`, `conserving`,
`charging`, `charging_interrupted` (all four = "connected/active"). The
session boundary is the transition between `connect_cable` and one of the
four active values:

Can be pasted directly into **Settings → Automations → Create Automation →
Edit in YAML** (no `automation:` wrapper, no `id:` needed – the UI creates
both itself; for `automations.yaml`/a package see the note below):

```yaml
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
              # Basis of the temperature/consumption analysis, see above.
              outside_temp_c: "{{ states('sensor.skoda_enyaq_outside_temperature') | float(default=None) }}"
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
            response_variable: lademonitor_session
          - action: notify.send_message
            target:
              entity_id: notify.mein_handy
            data:
              message: >-
                Ladevorgang zu Lademonitor gesendet: Start
                {{ lademonitor_session.start_time }} → Ende {{ lademonitor_session.end_time }},
                SoC {{ lademonitor_session.soc_start }}% → {{ lademonitor_session.soc_end }}%,
                {{ lademonitor_session.charging_type }}
```

Replace `notify.mein_handy` with your own Home Assistant app's notify
entity (Settings → Devices & Services → Entities → domain "notify").
`response_variable` accesses the newly introduced service response of
`end_charging_session` (see the blueprint section above) – this way the
start SoC/time/charging type don't need to be tracked separately for the
notification.

For `automations.yaml` or your own package, instead add it as a list entry
under the top-level key `automation:`, with an additional own `id:` (e.g.
`id: enyaq_lademonitor_push`) before `alias:` – the same fields, just one
indentation level deeper.

`energy_kwh` from the car is deliberately not sent along – MySkoda doesn't
provide a reliable kWh value, so the server estimates it from SoC delta ×
battery capacity. The wallbox meter (see above) is the better source. If `end_charging_session` is called for a
vehicle without `begin_charging_session` having run first (e.g. HA
restarted while the car was already charging), the service fails with a
clear error message instead of sending an incomplete record.

### Alternative: all values in a single call

If start and end values are already available together (e.g. a source that
only delivers the complete charging session after the fact, instead of
start and end as separate events), **`lademonitor.push_charging_session`**
pushes the charging session in a single call – no prior
`begin_charging_session` needed:

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
  outside_temp_c: "{{ session.outside_temp_c }}"
```

## Known limitation

Vehicles are loaded once when the integration is set up. A vehicle later
created in the server only appears after reloading the integration
(Settings → Devices & Services → Lademonitor → Reload).

## Dashboard cards

In the same style as the dashboard in the app/web UI: a tile grid with the
summary values, below it the AC/DC share as two gauges in blue/orange
(matching the AC/DC bar in the app/web UI). Provider pie charts and monthly
charts like in the app/web UI are further down under "Provider and monthly
charts" – native Lovelace cards are no longer sufficient for those.

There's no ready-made dashboard to import (unlike automation blueprints,
Lovelace has no URL import) – instead, below are individual cards to copy
into an existing dashboard: **Edit dashboard → Add card → top right
"Manual" → paste content**. First replace `sensor.skoda_enyaq_...`
everywhere via find & replace with your own entity IDs (Developer tools →
States → filter by your vehicle name) – for each additional vehicle, just
paste again with a different prefix.

The `entity_id`s below follow the Škoda Enyaq with English HA language
(device name "Skoda Enyaq" → prefix `skoda_enyaq`, the rest derived from
the English entity names in
[`translations/en.json`](custom_components/lademonitor/translations/en.json),
e.g. `total_sessions` → "Charging sessions" →
`sensor.skoda_enyaq_charging_sessions`) – with German HA language or a
different vehicle name, the actual IDs will differ, see the note above.

Tile grid with the seven summary values:

<details>
<summary>Show YAML</summary>

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: sensor.skoda_enyaq_charging_sessions
    name: Ladevorgänge
    icon: mdi:ev-station
  - type: tile
    entity: sensor.skoda_enyaq_total_energy_charged
    name: Gesamt kWh
    icon: mdi:lightning-bolt
  - type: tile
    entity: sensor.skoda_enyaq_total_cost
    name: Gesamtkosten
    icon: mdi:currency-eur
  - type: tile
    entity: sensor.skoda_enyaq_average_price_per_kwh
    name: "Ø Preis/kWh"
    icon: mdi:cash-multiple
  - type: tile
    entity: sensor.skoda_enyaq_average_consumption
    name: "Ø Verbrauch/100km"
    icon: mdi:speedometer
  - type: tile
    entity: sensor.skoda_enyaq_cost_per_100_km
    name: Preis/100km
    icon: mdi:currency-eur
  - type: tile
    entity: sensor.skoda_enyaq_total_distance_driven
    name: Gefahrene Kilometer
    icon: mdi:map-marker-distance
```

</details>

AC/DC share as two gauges side by side:

<details>
<summary>Show YAML</summary>

```yaml
type: horizontal-stack
cards:
  - type: gauge
    entity: sensor.skoda_enyaq_ac_share
    name: AC
    min: 0
    max: 100
    segments:
      - from: 0
        color: "#2196f3"
      - from: 100
        color: "#2196f3"
  - type: gauge
    entity: sensor.skoda_enyaq_dc_share
    name: DC
    min: 0
    max: 100
    segments:
      - from: 0
        color: "#ff9800"
      - from: 100
        color: "#ff9800"
```

</details>

### Provider and monthly charts (requires `apexcharts-card`)

For the provider pie charts and monthly bars from the app/web UI, native
Lovelace cards are no longer sufficient – the data sits as list-type
attributes (`monthly`, `by_provider`, see above) on the sensors, and only a
card with its own `data_generator` can turn that into a chart. For this,
first install [`apexcharts-card`](https://github.com/RomRider/apexcharts-card)
via HACS → Frontend (not part of Home Assistant itself).

Provider distribution, kWh and cost stacked (donut, as in the web UI).

<img src="docs/screenshots/anbieter-donut.jpg" alt="Provider distribution as two donut charts" width="439">

Important: with `chart_type: donut`/`pie`, **one series represents exactly
one slice** (`apexcharts-card` takes the last computed value per series) -
there is no automatism that fans a list like `by_provider` out into
multiple slices on its own, and a series name is a static YAML value (not
dynamically nameable via `data_generator`). Hence six fixed series slots
("Place 1"–"Place 5" + "Other"), whose **values** are automatically
populated from `by_provider` – the server already returns the list sorted
descending by kWh (see `Lademonitor-Server/CLAUDE.md`), so indices 0–4 are
automatically the five largest providers, everything from index 5 onward
automatically ends up in "Other". The entity is set once via a YAML anchor
(`&kwh_entity`/`&cost_entity`) and just referenced in the remaining series
(`*kwh_entity`/`*cost_entity`):

<details>
<summary>Show YAML</summary>

```yaml
type: vertical-stack
cards:
  - type: custom:apexcharts-card
    header:
      show: true
      title: kWh pro Anbieter
    chart_type: donut
    apex_config:
      chart:
        height: 200px
    series:
      - entity: &kwh_entity sensor.skoda_enyaq_total_kwh
        name: Platz 1
        data_generator: |
          const p = entity.attributes.by_provider[0];
          return [[Date.now(), p ? p.total_kwh : 0]];
      - entity: *kwh_entity
        name: Platz 2
        data_generator: |
          const p = entity.attributes.by_provider[1];
          return [[Date.now(), p ? p.total_kwh : 0]];
      - entity: *kwh_entity
        name: Platz 3
        data_generator: |
          const p = entity.attributes.by_provider[2];
          return [[Date.now(), p ? p.total_kwh : 0]];
      - entity: *kwh_entity
        name: Platz 4
        data_generator: |
          const p = entity.attributes.by_provider[3];
          return [[Date.now(), p ? p.total_kwh : 0]];
      - entity: *kwh_entity
        name: Platz 5
        data_generator: |
          const p = entity.attributes.by_provider[4];
          return [[Date.now(), p ? p.total_kwh : 0]];
      - entity: *kwh_entity
        name: Andere
        data_generator: |
          const p = entity.attributes.by_provider[5];
          return [[Date.now(), p ? p.total_kwh : 0]];
  - type: custom:apexcharts-card
    header:
      show: true
      title: Bezahlt pro Anbieter
    chart_type: donut
    apex_config:
      chart:
        height: 200px
    series:
      - entity: &cost_entity sensor.skoda_enyaq_total_cost
        name: Platz 1
        data_generator: |
          const p = entity.attributes.by_provider[0];
          return [[Date.now(), p ? p.total_cost : 0]];
      - entity: *cost_entity
        name: Platz 2
        data_generator: |
          const p = entity.attributes.by_provider[1];
          return [[Date.now(), p ? p.total_cost : 0]];
      - entity: *cost_entity
        name: Platz 3
        data_generator: |
          const p = entity.attributes.by_provider[2];
          return [[Date.now(), p ? p.total_cost : 0]];
      - entity: *cost_entity
        name: Platz 4
        data_generator: |
          const p = entity.attributes.by_provider[3];
          return [[Date.now(), p ? p.total_cost : 0]];
      - entity: *cost_entity
        name: Platz 5
        data_generator: |
          const p = entity.attributes.by_provider[4];
          return [[Date.now(), p ? p.total_cost : 0]];
      - entity: *cost_entity
        name: Andere
        data_generator: |
          const p = entity.attributes.by_provider[5];
          return [[Date.now(), p ? p.total_cost : 0]];
```

</details>

`by_provider` is already grouped server-side into top 5 + "Other"
(`sensor.py::_grouped_by_provider`, a port of the same rule as in the
app/web UI: "No provider" always ends up in "Other" regardless of its
size, never as its own slice) – so the card only needs to plainly read out
`by_provider[0..5]`, no more sorting/filtering/summing in the card YAML.

Good to know: the provider **order** (which provider is "Place 1") is thus
fully automatic – but the **name** in the legend/tooltip remains the
static placeholder "Place 1" etc., because `apexcharts-card` cannot derive
series names from `data_generator`. Anyone who wants to see the real
provider names in the legend has to manually replace "Place 1"–"Place 5"
with the currently leading providers (Developer tools → States, read off
the `by_provider` order) – only necessary again if the ranking changes; the
values/grouping themselves always stay correct.

`apex_config` passes through raw ApexCharts.js options (`chart.height`
above shrinks the diameter – ApexCharts sizes the circle to the smaller of
the two dimensions, a smaller value = a smaller circle). For a thinner ring
instead of a smaller circle, use `plotOptions.pie.donut.size` instead
(e.g. `"75%"`).

Cost per month (bars):

<img src="docs/screenshots/kosten-pro-monat.jpg" alt="Cost per month" width="437">

<details>
<summary>Show YAML</summary>

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Kosten pro Monat
graph_span: 180d
series:
  - entity: sensor.skoda_enyaq_total_cost
    type: column
    name: Kosten
    color: "#5b8def"
    data_generator: |
      return entity.attributes.monthly
        .slice()
        .reverse()
        .map(m => [new Date(m.month + "-01").getTime(), m.total_cost]);
```

</details>

kWh per month (bars):

<img src="docs/screenshots/kwh-pro-monat.jpg" alt="kWh per month" width="430">

<details>
<summary>Show YAML</summary>

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: kWh pro Monat
graph_span: 180d
series:
  - entity: sensor.skoda_enyaq_total_kwh
    type: column
    name: kWh
    color: "#4fd1a5"
    data_generator: |
      return entity.attributes.monthly
        .slice()
        .reverse()
        .map(m => [new Date(m.month + "-01").getTime(), m.total_kwh]);
```

</details>

Average consumption per month (bars, months with no computable value are
skipped):

<img src="docs/screenshots/verbrauch-pro-monat.jpg" alt="Average consumption per month" width="431">

<details>
<summary>Show YAML</summary>

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Ø Verbrauch pro Monat
graph_span: 180d
series:
  - entity: sensor.skoda_enyaq_average_consumption
    type: column
    name: kWh/100km
    color: "#9b7bde"
    data_generator: |
      return entity.attributes.monthly
        .slice()
        .reverse()
        .filter(m => m.avg_consumption_kwh_per_100km != null)
        .map(m => [new Date(m.month + "-01").getTime(), m.avg_consumption_kwh_per_100km]);
```

</details>

`graph_span` is a static value (not a `data_generator` expression) - there
is no documented way in `apexcharts-card` to automatically adjust the time
window to the number of months actually present. The `180d` above matches
the current amount of data (~6 months); if the monthly history grows
beyond that, the value has to be raised manually. Deliberately in days
instead of `6month`/`1year`: the `apexcharts-card` docs explicitly warn
that `month`/`year` units for `graph_span` can produce "inconsistent
result[s]" and recommend days. Choosing more generously (e.g. `730d` for
~2 years) saves having to adjust it again later, but shows some empty
space at the edge until then.

`monthly` comes from the server sorted descending (newest month first, see
`Lademonitor-Server/CLAUDE.md`) – the `.slice().reverse()` makes sure the
timeline runs left to right as in the web UI.
