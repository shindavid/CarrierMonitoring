#!/usr/bin/env python3
"""One-time migration: delete redundant config anchor rows and reclaim disk.

Before the change-only-config fix, every 5-minute poll wrote an anchor copy of
the entire near-static thermostat schedule (config.zone:*), so ~90% of the
`readings` table was duplicate config data. This deletes those anchors
(changed=0 config rows) while keeping every actual config change, then VACUUMs
to return the freed space to the filesystem.

Safe to re-run. Run with the ingest service STOPPED (VACUUM needs exclusive
access and rewrites the whole database).

    python deploy/prune_config_anchors.py /path/to/carriermon.sqlite
"""
from __future__ import annotations

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

    to_delete = conn.execute(
        "SELECT COUNT(*) FROM readings WHERE entity LIKE 'config%' AND changed=0"
    ).fetchone()[0]
    print(f"redundant config anchor rows to delete: {to_delete:,}")
    if to_delete == 0:
        print("nothing to do.")
        return

    t = time.time()
    with conn:
        conn.execute("DELETE FROM readings WHERE entity LIKE 'config%' AND changed=0")
    print(f"deleted in {time.time() - t:.1f}s; vacuuming (rewrites the DB, be patient)…")

    t = time.time()
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"vacuumed in {time.time() - t:.1f}s")

    remaining = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    after_bytes = sum(f.stat().st_size for f in db.parent.glob(db.name + "*"))
    print(f"readings rows remaining: {remaining:,}")
    print(f"on-disk: {before_bytes / 1e9:.2f} GB -> {after_bytes / 1e9:.2f} GB "
          f"(reclaimed {(before_bytes - after_bytes) / 1e9:.2f} GB)")


if __name__ == "__main__":
    main()
