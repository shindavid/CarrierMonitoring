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

### What gets stored (`data/carriermon.sqlite`)
- `raw_messages` — every payload from Carrier, verbatim.
- `readings` — every field of status + config, flattened, written when it **changes**
  (plus a full anchor row on each poll). `changed=1` rows are the change log.

### API
- `GET /api/systems`, `/api/fields`
- `GET /api/series?serial=&entity=zone:1&field=rt&start=&end=`
- `GET /api/events?serial=&start=&end=`
- `GET /api/dashboard?serial=&start=&end=`
