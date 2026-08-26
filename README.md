# carriermon

Historical monitoring and diagnostics for a Carrier Infinity / Bryant Evolution system.
See [PLAN.md](PLAN.md) for the background research and roadmap.

## Phase 1 — cloud logger (current)

```bash
python3.14 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # fill in your Carrier account credentials
.venv/bin/carriermon probe          # one-shot: shows thermostat model/firmware, IDU/ODU types, zones
.venv/bin/carriermon ingest         # logger: websocket push + full poll every 5 min (runs forever)
.venv/bin/carriermon web            # dashboard at http://localhost:8471 (port from CARRIERMON_PORT in .env)
```

Run `ingest` and `web` as two long-lived processes (e.g. two systemd units, or `tmux`).

### What gets stored (`data/carriermon.sqlite`)
- `raw_messages` — every payload from Carrier, verbatim.
- `readings` — every field of status + config, flattened, written when it **changes**
  (plus a full anchor row on each poll). `changed=1` rows are the change log.

### API
- `GET /api/systems`, `/api/fields`
- `GET /api/series?serial=&entity=zone:1&field=rt&start=&end=`
- `GET /api/events?serial=&start=&end=`
- `GET /api/dashboard?serial=&start=&end=`
