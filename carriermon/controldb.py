"""Controller state: a small SQLite file owned by *this checkout*.

Kept separate from the readings database on purpose: a dev checkout opens the
production readings read-only, but still needs somewhere writable for its own
(dry-run) controller settings, state and decision log. Both the web server (which
edits settings) and the loop host (ingest in prod, ``carriermon control`` in dev)
open this file read-write.

Tables
- control_settings: one row — on/off.
- control_zones:    one row per zone — its day/night desired temp and tolerable range,
                    and when day/night start. Zones without a row get the defaults.
- control_state:    one row — what the loop is doing / last wrote / why.
- control_log:      decisions, writes, overrides, errors; `user` names who made a change.
- users:            web logins (PBKDF2 hash + salt) with a role, see auth.py.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS control_settings (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    enabled     INTEGER NOT NULL DEFAULT 0,
    updated_ts  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS control_zones (
    entity      TEXT PRIMARY KEY,   -- 'zone:1' ...
    day_lo      REAL NOT NULL,      -- tolerable range [lo, hi], at least MIN_WIDTH wide
    day_d       REAL NOT NULL,      -- desired temp, lo <= d <= hi
    day_hi      REAL NOT NULL,
    night_lo    REAL NOT NULL,
    night_d     REAL NOT NULL,
    night_hi    REAL NOT NULL,
    day_start   TEXT NOT NULL,      -- HH:MM local time
    night_start TEXT NOT NULL
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
    loop_alive_ts       REAL,     -- heartbeat; the UI warns when this goes stale
    lean_side           TEXT,     -- 'above' | 'below' | NULL: every zone strictly past its desired temp
    lean_since          REAL      -- when that lean started
);
CREATE TABLE IF NOT EXISTS control_log (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    event   TEXT NOT NULL,   -- enabled | disabled | decision | write | override | error | check ...
    message TEXT NOT NULL,
    detail  TEXT,            -- optional JSON
    user    TEXT             -- who made the change (web login), NULL for the loop's own entries
);
CREATE INDEX IF NOT EXISTS control_log_ts ON control_log(ts);
CREATE TABLE IF NOT EXISTS users (
    name        TEXT PRIMARY KEY,
    role        TEXT NOT NULL,     -- admin | user
    pw_hash     TEXT NOT NULL,
    salt        TEXT NOT NULL,
    created_ts  REAL NOT NULL
);
"""

ZONE_FIELDS = ("day_lo", "day_d", "day_hi", "night_lo", "night_d", "night_hi", "day_start", "night_start")
ZONE_DEFAULTS = {"day_lo": 69.0, "day_d": 70.0, "day_hi": 71.0, "night_lo": 69.0, "night_d": 70.0, "night_hi": 71.0,
                 "day_start": "07:00", "night_start": "22:00"}
MIN_WIDTH = 2.0  # the thermostat's deadband: heat and cool setpoints can't be closer


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
        # CREATE TABLE IF NOT EXISTS won't add columns to tables from an earlier version.
        zone_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(control_zones)")}
        state_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(control_state)")}
        log_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(control_log)")}
        with self.conn:
            if "user" not in log_cols:
                self.conn.execute("ALTER TABLE control_log ADD COLUMN user TEXT")
            if zone_cols and "day_d" not in zone_cols:
                for period in ("day", "night"):
                    self.conn.execute(f"ALTER TABLE control_zones ADD COLUMN {period}_d REAL NOT NULL DEFAULT 70")
                    self.conn.execute(f"UPDATE control_zones SET {period}_d = ({period}_lo + {period}_hi) / 2")
            for column in ("lean_side TEXT", "lean_since REAL"):
                if column.split()[0] not in state_cols:
                    self.conn.execute(f"ALTER TABLE control_state ADD COLUMN {column}")
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO control_settings(id, enabled, updated_ts) VALUES (1, 0, ?)", (time.time(),)
            )
            self.conn.execute("INSERT OR IGNORE INTO control_state(id) VALUES (1)")
        # Files from before per-zone settings carried one global target (desired, ± 1
        # tolerable) and one day/night schedule; zones without a row inherit those.
        self.defaults = dict(ZONE_DEFAULTS)
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(control_settings)")}
        if "target_day" in have:
            row = self.conn.execute(
                "SELECT target_day, target_night, day_start, night_start FROM control_settings WHERE id=1"
            ).fetchone()
            if row:
                self.defaults.update(day_lo=row["target_day"] - 1, day_d=row["target_day"], day_hi=row["target_day"] + 1,
                                     night_lo=row["target_night"] - 1, night_d=row["target_night"], night_hi=row["target_night"] + 1,
                                     day_start=row["day_start"], night_start=row["night_start"])

    # -- settings (edited by the web UI) ---------------------------------
    def settings(self) -> dict:
        row = self.conn.execute("SELECT enabled, updated_ts FROM control_settings WHERE id=1").fetchone()
        return {"enabled": bool(row["enabled"]), "updated_ts": row["updated_ts"]}

    def set_enabled(self, enabled: bool) -> dict:
        """Turning the controller on also clears any override, which is how the user
        re-arms it after touching the thermostat."""
        with self.conn:
            self.conn.execute("UPDATE control_settings SET enabled=?, updated_ts=? WHERE id=1",
                              (int(enabled), time.time()))
            if enabled:
                self.conn.execute("UPDATE control_state SET override=NULL, override_ts=NULL WHERE id=1")
        return self.settings()

    def zone(self, entity: str) -> dict:
        row = self.conn.execute("SELECT * FROM control_zones WHERE entity=?", (entity,)).fetchone()
        return {k: row[k] for k in ZONE_FIELDS} if row else dict(self.defaults)

    def zones(self, entities: list[str]) -> dict[str, dict]:
        return {e: self.zone(e) for e in entities}

    def update_zone(self, entity: str, **fields: Any) -> dict:
        """Change some of a zone's ZONE_FIELDS. Per period: lo <= d <= hi and hi - lo >= MIN_WIDTH."""
        current = self.zone(entity)
        new = {**current, **{k: v for k, v in fields.items() if k in ZONE_FIELDS and v is not None}}
        for period in ("day", "night"):
            lo, d, hi = new[f"{period}_lo"], new[f"{period}_d"], new[f"{period}_hi"]
            if hi - lo < MIN_WIDTH:
                raise ValueError(f"{period} range must be at least {MIN_WIDTH:g}° wide")
            if not lo <= d <= hi:
                raise ValueError(f"{period} desired temp must be within {lo:g}–{hi:g}")
        with self.conn:
            self.conn.execute(
                f"INSERT OR REPLACE INTO control_zones(entity, {', '.join(ZONE_FIELDS)})"
                f" VALUES (?{', ?' * len(ZONE_FIELDS)})",
                (entity, *(new[k] for k in ZONE_FIELDS)),
            )
            self.conn.execute("UPDATE control_settings SET updated_ts=? WHERE id=1", (time.time(),))
        return new

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
    def log(self, event: str, message: str, detail: Any = None, user: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO control_log(ts, event, message, detail, user) VALUES (?,?,?,?,?)",
                (time.time(), event, message, _j(detail), user),
            )

    def recent_log(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, event, message, detail, user FROM control_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{**dict(r), "detail": _unj(r["detail"])} for r in rows]

    # -- users (web logins) ----------------------------------------------
    def has_users(self) -> bool:
        return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def list_users(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT name, role, created_ts FROM users ORDER BY name")]

    def add_user(self, name: str, password: str, role: str) -> None:
        from .auth import ROLES, USERNAME_RE, hash_password
        if not USERNAME_RE.match(name or ""):
            raise ValueError("user name: 1-32 letters, digits, '.', '_' or '-'")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not password:
            raise ValueError("password must not be empty")
        pw_hash, salt = hash_password(password)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO users(name, role, pw_hash, salt, created_ts) VALUES (?,?,?,?,?)",
                              (name, role, pw_hash, salt, time.time()))

    def set_password(self, name: str, password: str) -> None:
        from .auth import hash_password
        if not password:
            raise ValueError("password must not be empty")
        pw_hash, salt = hash_password(password)
        with self.conn:
            if self.conn.execute("UPDATE users SET pw_hash=?, salt=? WHERE name=?", (pw_hash, salt, name)).rowcount == 0:
                raise KeyError(name)

    def remove_user(self, name: str) -> None:
        with self.conn:
            if self.conn.execute("DELETE FROM users WHERE name=?", (name,)).rowcount == 0:
                raise KeyError(name)

    def verify_user(self, name: str, password: str) -> str | None:
        """The user's role if the password is right, else None."""
        from .auth import check_password
        row = self.conn.execute("SELECT role, pw_hash, salt FROM users WHERE name=?", (name,)).fetchone()
        if row is None or not check_password(password, row["pw_hash"], row["salt"]):
            return None
        return row["role"]

    def prune_log(self, keep_days: float = 30) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM control_log WHERE ts < ?", (time.time() - keep_days * 86400,))
