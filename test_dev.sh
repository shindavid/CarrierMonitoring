#!/usr/bin/env bash
# Run a throwaway dashboard from THIS checkout on localhost, browsing the production
# database read-only. Runs in the foreground; Ctrl+C stops it. Never starts ingest.
#
#   ./test_dev.sh [path-to-prod-db]     # first run creates .venv and a dev .env
#
# Safety model: this checkout's .env is marked CARRIERMON_DEV=1, which makes
# `carriermon ingest` refuse to run here, and the web server only ever opens the
# database read-only — so nothing done from this checkout can affect production.
set -euo pipefail
cd "$(dirname "$0")"
PORT_DEFAULT=8499

if [ -f .env ] && ! grep -q '^CARRIERMON_DEV=1' .env; then
  echo "refusing: .env here is not marked CARRIERMON_DEV=1 — this looks like the production checkout." >&2
  echo "Run this from a separate clone (git clone <repo> ../CarrierMonitoring-dev)." >&2
  exit 1
fi

if [ ! -f .env ]; then
  DB=${1:-$(cd .. && pwd)/CarrierMonitoring/data/carriermon.sqlite}
  [ -f "$DB" ] || { echo "production database not found at $DB — pass its path as the first argument" >&2; exit 1; }
  cat > .env <<ENV
# Dev checkout: read-only view of the production database. No Carrier login needed.
CARRIERMON_DEV=1
CARRIERMON_DB=$DB
CARRIERMON_HOST=127.0.0.1
CARRIERMON_PORT=$PORT_DEFAULT
# No auth on the local dev server
CARRIERMON_AUTH_USER=
CARRIERMON_AUTH_PASSWORD=
ENV
  chmod 600 .env
  echo "created dev .env (db: $DB)"
fi

[ -x .venv/bin/carriermon ] || { echo "creating .venv…"; python3 -m venv .venv && .venv/bin/pip install -q -e .; }
PORT=$(grep '^CARRIERMON_PORT=' .env | cut -d= -f2- || true)
echo "dev dashboard: http://localhost:${PORT:-$PORT_DEFAULT}/   (Ctrl+C to stop)"
exec .venv/bin/carriermon web
