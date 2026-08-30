"""SQLite storage.

Tables
- raw_messages: every payload we ever received, verbatim (debug gold).
- readings:     change-tracked flattened values. A row is written when a value
                differs from the last stored value for that (entity, field), and
                additionally on every scheduled poll so series have anchors.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    source  TEXT NOT NULL,          -- cloud:load | cloud:poll | cloud:ws | bus:mqtt
    serial  TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_messages_ts ON raw_messages(ts);

CREATE TABLE IF NOT EXISTS readings (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    serial     TEXT NOT NULL,
    entity     TEXT NOT NULL,       -- system | zone:1 | idu | odu | config | config.zone:1 ...
    field      TEXT NOT NULL,
    value_num  REAL,
    value_text TEXT,
    changed    INTEGER NOT NULL,    -- 1 if this row differs from the previous value
    source     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_lookup ON readings(serial, entity, field, ts);
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS readings_changed ON readings(changed, ts);
"""


class Store:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        """Open the database. ``read_only`` opens it with SQLite's ``mode=ro`` so the
        connection cannot modify it at all (used by the web server, and what lets a
        dev checkout browse the production database safely)."""
        if read_only:
            if not path.exists():
                raise FileNotFoundError(f"database not found: {path}")
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            # Wait rather than fail instantly if the read-only web connection holds a
            # lock (matters for VACUUM during prune()).
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row

    # -- raw -------------------------------------------------------------
    def add_raw(self, source: str, serial: str | None, payload: Any, ts: float | None = None) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        with self.conn:
            self.conn.execute(
                "INSERT INTO raw_messages(ts, source, serial, payload) VALUES (?,?,?,?)",
                (ts or time.time(), source, serial, text),
            )

    # -- readings --------------------------------------------------------
    def last_values(self, serial: str) -> dict[tuple[str, str], Any]:
        """Most recent stored value per (entity, field) for a system."""
        rows = self.conn.execute(
            """
            SELECT entity, field, value_num, value_text FROM readings
            WHERE serial = ? AND id IN (
                SELECT MAX(id) FROM readings WHERE serial = ? GROUP BY entity, field
            )
            """,
            (serial, serial),
        )
        return {
            (r["entity"], r["field"]): (r["value_num"] if r["value_num"] is not None else r["value_text"])
            for r in rows
        }

    def add_readings(self, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        with self.conn:
            self.conn.executemany(
                "INSERT INTO readings(ts, serial, entity, field, value_num, value_text, changed, source)"
                " VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    # -- retention -------------------------------------------------------
    def prune(self, retention_days: float, vacuum: bool = True) -> int:
        """Delete rows older than the retention window from both tables and return
        how many were removed. Deleting alone only frees pages for reuse (the file
        does not shrink); ``vacuum`` rewrites the database to actually give the space
        back to the filesystem."""
        cutoff = time.time() - retention_days * 86400
        with self.conn:
            deleted = self.conn.execute("DELETE FROM raw_messages WHERE ts < ?", (cutoff,)).rowcount
            deleted += self.conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount
        if vacuum and deleted:
            self.conn.execute("VACUUM")  # must run outside a transaction (the block above committed)
            # In WAL mode VACUUM's rewrite lands in the -wal file; checkpoint it back
            # so the space is actually returned to the filesystem.
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    # -- queries for the web UI -----------------------------------------
    def fields(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT serial, entity, field,
                   SUM(value_num IS NOT NULL) AS numeric_rows,
                   COUNT(*) AS rows_total, MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM readings GROUP BY serial, entity, field ORDER BY entity, field
            """
        )
        return [dict(r) for r in rows]

    def series(self, serial: str, entity: str, field: str, start: float, end: float) -> list[dict]:
        # Include the last value before `start` so step charts have a starting level.
        prior = self.conn.execute(
            "SELECT ts, value_num, value_text FROM readings WHERE serial=? AND entity=? AND field=? AND ts<? "
            "ORDER BY ts DESC LIMIT 1",
            (serial, entity, field, start),
        ).fetchone()
        rows = self.conn.execute(
            "SELECT ts, value_num, value_text FROM readings WHERE serial=? AND entity=? AND field=? "
            "AND ts>=? AND ts<=? ORDER BY ts",
            (serial, entity, field, start, end),
        ).fetchall()
        out = [dict(prior)] if prior else []
        out.extend(dict(r) for r in rows)
        return out

    def events(self, serial: str | None, start: float, end: float, limit: int = 2000) -> list[dict]:
        sql = "SELECT ts, serial, entity, field, value_num, value_text, source FROM readings WHERE changed=1 AND ts>=? AND ts<=?"
        args: list[Any] = [start, end]
        if serial:
            sql += " AND serial=?"
            args.append(serial)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def serials(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT serial FROM readings")]

    def zones(self, serial: str) -> list[dict]:
        """Enabled zones with display names (names live in config, enabled flag in status)."""
        latest = """
            SELECT entity, value_text FROM readings
            WHERE serial=? AND entity LIKE ? AND field=?
              AND id IN (SELECT MAX(id) FROM readings WHERE serial=? AND entity LIKE ? AND field=? GROUP BY entity)
        """
        names = {e.replace("config.", ""): v for e, v in
                 self.conn.execute(latest, (serial, "config.zone:%", "name", serial, "config.zone:%", "name"))}
        enabled = {e: v for e, v in
                   self.conn.execute(latest, (serial, "zone:%", "enabled", serial, "zone:%", "enabled"))}
        zones = [e for e in set(names) | set(enabled) if enabled.get(e, "on") == "on"]
        zones.sort(key=lambda e: int(e.split(":")[1]) if e.split(":")[1].isdigit() else 0)
        return [{"entity": e, "name": names.get(e) or e} for e in zones]
