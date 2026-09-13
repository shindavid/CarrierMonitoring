"""Controller state: a small SQLite file owned by *this checkout*.

Kept separate from the readings database on purpose: a dev checkout opens the
production readings read-only, but still needs somewhere writable for its own
(dry-run) controller settings, state and decision log. Both the web server (which
edits settings) and the loop host (ingest in prod, ``carriermon control`` in dev)
open this file read-write.

Tables
- control_settings: one row — what the user asked for (enabled, target).
- control_state:    one row — what the loop is doing / last wrote / why.
- control_log:      decisions, writes, overrides, errors (newest first in the UI).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS control_settings (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    enabled      INTEGER NOT NULL DEFAULT 0,
    target_day   REAL    NOT NULL DEFAULT 70,
    target_night REAL    NOT NULL DEFAULT 70,
    day_start    TEXT    NOT NULL DEFAULT '07:00',   -- HH:MM local time
    night_start  TEXT    NOT NULL DEFAULT '22:00',
    updated_ts   REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS control_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    mode                TEXT,     -- mode the controller last commanded: heat | cool
    mode_since          REAL,     -- when that mode was commanded
    rule                TEXT,     -- the rule that chose it
    expected            TEXT,     -- JSON: what we last wrote (mode + per-zone setpoints); NULL = nothing written
    written_ts          REAL,     -- when `expected` was written (override grace period starts here)
    applied_settings_ts REAL,     -- control_settings.updated_ts that `expected` was built from
    last_decision_mode  TEXT,     -- last mode the rules asked for (to log decisions only on change)
    last_eval_ts        REAL,
    last_eval           TEXT,     -- JSON snapshot of inputs + decision from the last tick
    override            TEXT,     -- why the controller switched itself off, or NULL
    override_ts         REAL,
    dry_run             INTEGER,
    loop_alive_ts       REAL      -- heartbeat; the UI warns when this goes stale
);
CREATE TABLE IF NOT EXISTS control_log (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    event   TEXT NOT NULL,   -- enabled | disabled | decision | write | override | error
    message TEXT NOT NULL,
    detail  TEXT             -- optional JSON
);
CREATE INDEX IF NOT EXISTS control_log_ts ON control_log(ts);
"""


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _unj(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class ControlStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")  # web + loop both write
        self.conn.executescript(SCHEMA)
        # Day/night targets replaced the single `target` column; upgrade older files in
        # place (CREATE TABLE IF NOT EXISTS won't add columns) and carry the old value over.
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(control_settings)")}
        if "target_day" not in have:
            with self.conn:
                for column, spec in (("target_day", "REAL NOT NULL DEFAULT 70"), ("target_night", "REAL NOT NULL DEFAULT 70"),
                                     ("day_start", "TEXT NOT NULL DEFAULT '07:00'"), ("night_start", "TEXT NOT NULL DEFAULT '22:00'")):
                    self.conn.execute(f"ALTER TABLE control_settings ADD COLUMN {column} {spec}")
                if "target" in have:
                    self.conn.execute("UPDATE control_settings SET target_day=target, target_night=target")
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO control_settings(id, enabled, updated_ts) VALUES (1, 0, ?)",
                (time.time(),),
            )
            self.conn.execute("INSERT OR IGNORE INTO control_state(id) VALUES (1)")

    # -- settings (edited by the web UI) ---------------------------------
    FIELDS = ("target_day", "target_night", "day_start", "night_start")

    def settings(self) -> dict:
        row = self.conn.execute(
            "SELECT enabled, target_day, target_night, day_start, night_start, updated_ts FROM control_settings WHERE id=1"
        ).fetchone()
        out = {k: row[k] for k in self.FIELDS}
        out.update(enabled=bool(row["enabled"]), updated_ts=row["updated_ts"])
        return out

    def update_settings(self, enabled: bool | None = None, **fields: Any) -> dict:
        """Apply the user's edits (any of FIELDS). Turning the controller on also clears
        any override, which is how the user re-arms it after touching the thermostat."""
        now = time.time()
        with self.conn:
            if enabled is not None:
                self.conn.execute("UPDATE control_settings SET enabled=?, updated_ts=? WHERE id=1", (int(enabled), now))
                if enabled:
                    self.conn.execute("UPDATE control_state SET override=NULL, override_ts=NULL WHERE id=1")
            for key, value in fields.items():
                if key not in self.FIELDS or value is None:
                    continue
                self.conn.execute(f"UPDATE control_settings SET {key}=?, updated_ts=? WHERE id=1", (value, now))
        return self.settings()

    # -- state (owned by the loop) ---------------------------------------
    def state(self) -> dict:
        row = dict(self.conn.execute("SELECT * FROM control_state WHERE id=1").fetchone())
        row["expected"] = _unj(row["expected"])
        row["last_eval"] = _unj(row["last_eval"])
        row["dry_run"] = bool(row["dry_run"]) if row["dry_run"] is not None else None
        return row

    def set_state(self, **fields: Any) -> None:
        for key in ("expected", "last_eval"):
            if key in fields:
                fields[key] = _j(fields[key])
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE control_state SET {assignments} WHERE id=1", tuple(fields.values()))

    def heartbeat(self, dry_run: bool) -> None:
        self.set_state(loop_alive_ts=time.time(), dry_run=int(dry_run))

    def trip_override(self, reason: str) -> None:
        """A human changed something we own: switch off and remember why."""
        now = time.time()
        with self.conn:
            self.conn.execute("UPDATE control_settings SET enabled=0, updated_ts=? WHERE id=1", (now,))
            self.conn.execute(
                "UPDATE control_state SET override=?, override_ts=?, expected=NULL, written_ts=NULL WHERE id=1",
                (reason, now),
            )

    # -- log -------------------------------------------------------------
    def log(self, event: str, message: str, detail: Any = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO control_log(ts, event, message, detail) VALUES (?,?,?,?)",
                (time.time(), event, message, _j(detail)),
            )

    def recent_log(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, event, message, detail FROM control_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{**dict(r), "detail": _unj(r["detail"])} for r in rows]

    def prune_log(self, keep_days: float = 30) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM control_log WHERE ts < ?", (time.time() - keep_days * 86400,))
