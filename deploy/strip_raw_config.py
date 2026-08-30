#!/usr/bin/env python3
"""One-time migration: drop redundant config blobs from historical raw_messages.

Before the change-only-config fix, every poll's raw payload embedded the full
~34 KB config, which was ~89% of raw_messages though config changes only rarely.
This walks the cloud:poll / cloud:load raws in time order and removes the
``config`` key from every payload whose config is identical to the previously
kept one — preserving each distinct config version (the last config-bearing raw
before any timestamp still reconstructs config at that time), then VACUUMs.

Safe to re-run. Run with the ingest service STOPPED.

    python deploy/strip_raw_config.py /path/to/carriermon.sqlite
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <db_path>")
    db = Path(sys.argv[1])
    if not db.exists():
        sys.exit(f"database not found: {db}")

    before_bytes = sum(f.stat().st_size for f in db.parent.glob(db.name + "*"))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA busy_timeout=10000")

    rows = conn.execute(
        "SELECT id, serial, payload FROM raw_messages "
        "WHERE source IN ('cloud:poll','cloud:load') ORDER BY ts"
    ).fetchall()

    last_config: dict[str, str] = {}  # per serial, last KEPT config (canonical JSON)
    updates: list[tuple[str, int]] = []
    kept = 0
    for _id, serial, payload in rows:
        try:
            doc = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if "config" not in doc:
            continue
        canon = json.dumps(doc["config"], sort_keys=True)
        if last_config.get(serial) != canon:
            last_config[serial] = canon  # first sighting or a real change -> keep
            kept += 1
            continue
        del doc["config"]  # redundant copy -> strip
        updates.append((json.dumps(doc, default=str), _id))

    print(f"config-bearing raws: {kept + len(updates):,}  "
          f"(keeping {kept} distinct versions, stripping {len(updates):,})")
    if not updates:
        print("nothing to do.")
        return

    t = time.time()
    with conn:
        conn.executemany("UPDATE raw_messages SET payload=? WHERE id=?", updates)
    print(f"stripped in {time.time() - t:.1f}s; vacuuming (rewrites the DB)…")

    t = time.time()
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"vacuumed in {time.time() - t:.1f}s")

    after_bytes = sum(f.stat().st_size for f in db.parent.glob(db.name + "*"))
    print(f"on-disk: {before_bytes / 1e9:.3f} GB -> {after_bytes / 1e9:.3f} GB "
          f"(reclaimed {(before_bytes - after_bytes) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
