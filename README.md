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

### Running as services / hosting

`deploy/install.sh` installs `carriermon-ingest` and `carriermon-web` as `systemctl --user`
units (paths derived from the checkout) and starts them; `./restart.sh` restarts both after
pulling changes.

To expose the dashboard, keep `CARRIERMON_HOST=127.0.0.1` and put a TLS-terminating proxy or
tunnel in front of it (e.g. a Cloudflare Tunnel ingress rule
`hostname: <your host> → service: http://127.0.0.1:8471`). Set `CARRIERMON_AUTH_USER` /
`CARRIERMON_AUTH_PASSWORD` in `.env` to require HTTP Basic Auth on every request. Anything
site-specific (tunnel configs, hostnames) can live in `deploy/local/`, which is gitignored.

### Developing without touching production

Make a second clone and run `./test_dev.sh` from it. The first run creates a `.venv` and a
dev `.env` (`CARRIERMON_DEV=1`, pointing at the production database next door, no auth) and
then serves http://localhost:8499 until Ctrl+C. Dev checkouts can only *read*: the web server
opens the database read-only, and `carriermon ingest` refuses to run when `CARRIERMON_DEV=1`.
Note the dashboard HTML is read from disk per request, so in the **production** checkout an
edit to `carriermon/static/index.html` is live immediately; Python changes need `./restart.sh`.

### Controller (`/control`)

The thermostat's Auto mode follows the cooling demand of the warmest zones and can
ignore a zone sitting under its heat setpoint. The built-in controller replaces just that
heat-or-cool decision: every minute it looks at all zone temps and the outdoor temp,
picks heat or cool, and holds **every zone at the same target T** (the thermostat's own
per-zone demand then steers the dampers). Rules, in order:

1. any zone ≥ T+2 and outdoor > T−10 → cool; any zone ≤ T−2 and outdoor < T+10 → heat
   (both → the larger error wins, equal → outdoor decides)
2. no zone out of band: outdoor ≥ T+2 → cool, outdoor ≤ T−2 → heat, else keep the current mode

It writes cool T / heat T−2 in cool mode and heat T / cool T+2 in heat mode (the
thermostat's deadband). Any change made at the thermostat or in the Carrier app to the
mode, a setpoint or a hold **switches the controller off**; press ON on `/control` to
re-arm it.

Where it runs: in the production checkout the loop is hosted by `carriermon ingest`
(the process holding the Carrier session); `CARRIERMON_CONTROL_DRY_RUN=1` makes it log
without writing. In a dev checkout run `.venv/bin/carriermon control` — always dry-run —
alongside `./test_dev.sh`, and use the dev dashboard's `/control` page. Settings, state
and the decision log live in `data/control.sqlite` of *that* checkout, so a dev run never
touches production's controller.

### What gets stored (`data/carriermon.sqlite`)
- `raw_messages` — every payload from Carrier, verbatim.
- `readings` — every field of status + config, flattened, written when it **changes**
  (plus a full anchor row on each poll). `changed=1` rows are the change log.

### API
- `GET /api/systems`, `/api/fields`
- `GET /api/series?serial=&entity=zone:1&field=rt&start=&end=`
- `GET /api/events?serial=&start=&end=`
- `GET /api/dashboard?serial=&start=&end=`
- `GET /api/control`, `POST /api/control {"enabled": bool, "target": 55..85}` — controller settings, state, log
