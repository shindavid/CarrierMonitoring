#!/usr/bin/env python3
"""One-time backfill: restore config rows (zone names, setpoint limits, program)
that the retention prune deleted.

Config is recorded change-only, so each config value is written once and then
never again unless it changes. The 7-day retention prune deleted rows by
timestamp regardless of the ``changed`` flag, so the one-time config rows aged
out and — because config rarely changes — were never re-emitted. The dashboard
then had no ``config.zone:N/name`` rows and fell back to showing "zone:1" etc.

This reconstructs the current config from the most recent config-bearing raw
message and re-inserts only the (entity, field) pairs whose latest stored value
is missing or stale — the same change-only rule the ingest uses, so it is safe
to re-run and inserts nothing once the config is present.

Pair this with the prune() fix that exempts ``config%`` rows from the retention
window, otherwise the backfilled rows will age out again.

    python deploy/backfill_config_names.py /path/to/carriermon.sqlite
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from carriermon.db import Store
from carriermon.normalize import diff_rows, snapshot


def latest_config(conn: sqlite3.Connection, serial: str) -> tuple[dict, float] | None:
    """Return (config_raw, ts) from the newest raw message that carried config."""
    for ts, payload in conn.execute(
        "SELECT ts, payload FROM raw_messages WHERE serial=? ORDER BY id DESC",
        (serial,),
    ):
        try:
            config = json.loads(payload).get("config")
        except (ValueError, AttributeError):
            continue
        if config:
            return config, ts
    return None


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <db_path>")
    db = Path(sys.argv[1])
    if not db.exists():
        sys.exit(f"database not found: {db}")

    store = Store(db)  # read-write
    serials = [r[0] for r in store.conn.execute("SELECT DISTINCT serial FROM readings")]
    if not serials:
        print("no systems in database; nothing to do.")
        return

    total = 0
    for serial in serials:
        found = latest_config(store.conn, serial)
        if not found:
            print(f"{serial}: no raw message carrying config found — skipping")
            continue
        config_raw, ts = found
        values = snapshot({}, config_raw)  # config.* only; empty status flattens to nothing
        last = store.last_values(serial)
        rows, changed = diff_rows(serial, values, last, source="backfill", force_all=False, ts=ts)
        inserted = store.add_readings(rows)
        total += inserted
        names = sorted(
            f"{e.split(':')[1]}={values[(e, f)]!r}"
            for (e, f) in values if f == "name" and e.startswith("config.zone:")
        )
        print(f"{serial}: inserted {inserted} config row(s) ({changed} changed); "
              f"zone names now recorded: {', '.join(names) or '(none in config)'}")

    print(f"done; {total} row(s) inserted total.")


if __name__ == "__main__":
    main()
