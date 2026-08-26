# Carrier Infinity Monitoring — Plan

Goal: a self-hosted web app (mobile-friendly) that records every zone/mode/fan/setpoint
change and every thermostat + equipment reading from the home Carrier Infinity system,
graphs them historically, and collects evidence for a suspected reversing-valve fault.

## What the four projects actually are (from reading the source, Aug 2026)

| Project | Data path | Gives us | Status |
|---|---|---|---|
| **dahlb/ha_carrier** (+ `carrier_api`) | Carrier cloud: GraphQL at `dataservice.infinity.iot.carrier.com` + push websocket `realtime.infinity.iot.carrier.com` | Per zone: `rt`, `rh`, `htsp`, `clsp`, `fan`, `hold`, `otmr`, activity, `zoneconditioning`, `damperposition`, occupancy. System: `oat`, `mode`, `idu`/`odu` `{type, opstat, cfm, statpress, blwrpm}`, filter/humidifier/UV, energy. Config (schedules, heat source). | Very active (commits 2026-08-24). `carrier_api` is a standalone PyPI lib, **needs Python ≥3.14**. HA-only bits live in `ha_carrier`. Default poll 30 min; websocket pushes zone status/config changes in near-real-time. |
| **nebulous/infinitude** | (a) MITM proxy of the thermostat's cloud traffic — thermostat must be pointed at it; (b) optional passive RS-485 (ABCD bus) monitor | (a) same status/config XML as the cloud, every 12–20 s ping; (b) raw bus registers | Active, but **the proxy is broken on thermostat firmware ≥ 4.17 / CESR131626** (cloud moved to MQTT; wiki compat matrix + issue #148). No long-term logging (it's on `todo.txt`). Perl/Mojolicious. |
| **MizterB/homeassistant-infinitude-beyond** | HA integration on top of an Infinitude server | Nice mapping of `idu.opstat`/`odu.opstat`/`odu.opmode` → furnace/heat-pump stage & mode sensors | Requires Infinitude, so inherits its firmware limitation. Its `infinitude/api.py` client is reusable but adds nothing we can't get from `carrier_api` or Infinitude's REST API directly. |
| **nebulous/infinitesp** | ESP32 + RS-485 transceiver wired to the ABCD bus (A/B/GND at the thermostat or furnace board), ESPHome firmware. Can run **passive** (`sam_address: 0`) — no commissioning, no bus writes. | Everything the equipment itself reports: ODU stage (commanded + actual), **ODU operating mode**, compressor RPM/frequency, EXV position, **coil / suction / discharge / outdoor temps**, superheat & subcooling target/actual, blower RPM, CFM, damper positions, zone temp/RH, **thermostat fault log (reg 0x4202)**. Publishes to HA via ESPHome API or MQTT. | Very active (commit 2026-08-26). Verified mostly on the author's variable-speed system; 2-stage ODUs (24ANA/25HNB family) use table 0x3E with fewer fields. |

Related: `acd/infinitive` (Go, RS-485, older non-Touch stats; unmaintained since 2023) — useful only as protocol reference.

## Why this matters for the reversing-valve hypothesis

The cloud API only tells you *what the thermostat thinks it commanded*: system `mode`,
per-zone `zoneconditioning` (active heat/cool), and `odu.opstat` (stage / % output).
It cannot show whether the refrigerant circuit actually reversed. The bus can:

- Cool call with compressor running but **coil temp climbing / discharge temp pattern of heating**, or the opposite in a heat call → direct reversing-valve evidence.
- Suction/discharge temps and superheat/subcooling **during the first minutes after a mode switch** are exactly where a sticking valve shows up.
- Thermostat fault log (0x4202) will show any equipment-reported faults with timestamps.
- Indirect (available from cloud alone): mode = cool, `zoneconditioning` = active cooling, ODU stage > 0, blower running, yet zone `rt` rises for 20+ min — plus outdoor temp for context.

So: cloud data gets a usable timeline running *today*; the bus tap is what turns "it
didn't cool" into "the valve didn't reverse".

## Findings from the first live run (2026-08-26)

`carriermon probe` against the real account:

- Thermostat **SYSTXCCITC01-B**, firmware **CESR131626-04.79** → the Infinitude proxy is
  confirmed *not* an option (matches the compat matrix).
- Outdoor unit **24VNA948A00300** = Infinity 24VNA9 variable-speed **air conditioner**
  (`odutype=multistgac`, status type `proteusac`, opstat reported as `Stage 1..5`).
- Indoor unit **59MN7B080C211120** = Infinity modulating **gas furnace**
  (`fueltype=gas`, `heatsource=system`).
- 4 enabled zones (2nd Floor, 1st Floor, Nursery, Hansen's room) of 8.
- Websocket push works: 5 messages in the first 40 s, including an ODU stage change.

**Consequence for the hypothesis:** this is an AC + gas furnace system, not a heat pump.
There is no reversing valve. An intermittent failure to switch between heating and
cooling must come from elsewhere — e.g. the thermostat's auto-mode changeover logic
(deadband `cfgdead`, cycles-per-hour, staging timers), the furnace/AC interlock, or
communication faults. The same logging approach still applies; the analysis views
should focus on **mode/conditioning transitions vs. what the IDU (furnace) and ODU (AC)
actually report**, and on the thermostat fault log (bus register 0x4202 via infinitesp).

## Recommended approach

**Do not build on Infinitude's proxy** unless the thermostat is on old firmware (check
Menu → Service → Model/Serial/Software; anything ≥4.17 rules it out). Skip
infinitude-beyond entirely (HA-only, Infinitude-only).

Build our own small service with two ingestion sources feeding one time-series store:

```
 carrier_api (cloud, websocket+poll) ─┐
                                      ├─► ingest → SQLite (raw JSON + normalized samples/events) ─► FastAPI ─► web UI (uPlot charts)
 infinitesp (ESP32 on ABCD bus) → MQTT ┘
```

### Phase 1 — cloud logger + dashboard (no hardware, start now)
1. `ingest/cloud.py`: use `carrier_api` (Python 3.14 venv). Login, `load_data()`, start the
   websocket listener, and poll `get_systems()` every ~5 min as a fallback (ha_carrier uses
   30 min + websocket; we want a finer floor).
2. Persist **every raw payload** (initial load, each websocket message, each poll) verbatim
   in a `raw_messages` table — this is the debug gold; schema can be re-derived later.
3. Normalize into:
   - `samples(ts, entity, field, value)` — numeric readings: zone rt/rh/htsp/clsp/damper, oat, cfm, blwrpm, statpress, odu opstat %.
   - `events(ts, entity, field, old, new, source)` — discrete changes: system mode, zone fan/hold/activity/conditioning, idu/odu opstat strings, config edits. One row per change so the "every change" requirement is a simple query.
4. `web/`: FastAPI serving JSON + a static single-page UI (uPlot or Plotly) with: per-zone temp vs setpoints with mode/fan/conditioning shaded bands; system-level ODU/IDU stage + OAT; an events timeline table; date-range picker. Responsive so it works on a phone. (Grafana on the SQLite is a viable shortcut for graphs if we want it faster.)
5. Run as a systemd service / Docker on the always-on box.

### Phase 2 — RS-485 tap via infinitesp (needs ~$20–40 hardware)
1. Hardware: any ESP32 + RS-485 module (README: MAX13487E-class auto-direction boards work cleanly; the Waveshare relay board needs the `uart_rmtx` workaround). Wire A/B/GND to the ABCD terminals.
2. Flash infinitesp with `sam_address: 0` and `zone_controller_address: 0` (**passive** — no commissioning, nothing written to the bus, and no conflict if a physical SAM/zone board exists).
3. Point it at an MQTT broker (Mosquitto); `ingest/bus.py` subscribes and writes the same `samples`/`events` tables (ODU mode/stage, compressor RPM, coil/suction/discharge temps, superheat/subcooling, EXV, blower RPM, fault log).
4. Add a "mode transition" view: for each heat↔cool switch, overlay the first 30 min of ODU mode, compressor RPM, coil/suction/discharge temps, and zone temps — the reversing-valve fault should be visible here.
5. If the ODU is a 2-stage unit rather than variable-speed, use infinitesp's `raw_register` sensors (table 0x3E) and contribute decodes back upstream.

### Phase 3 — analysis helpers
- Automatic anomaly flags: "cooling demanded + compressor running + zone temp rising N min", "ODU mode ≠ system mode", fault-log entries.
- CSV/JSON export of a time window for sharing with the HVAC tech.

## Open questions

Questions 1–2 were answered by `probe` (see Findings). Still open:
1. Do you already run Home Assistant / an MQTT broker? (If HA exists, ha_carrier + infinitesp + HA recorder is a zero-code alternative for logging, though the custom UI is still needed for the transition-analysis views.)
2. Where will this run (always-on Linux box with Python 3.14 available, or Docker)?
