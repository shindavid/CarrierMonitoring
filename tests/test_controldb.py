"""ControlStore: settings, per-zone config with validation, state, log, migrations."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from carriermon.controldb import MIN_WIDTH, ZONE_DEFAULTS, ZONE_FIELDS, ControlStore


class TestSettings:
    def test_fresh_store_is_off(self, control: ControlStore):
        s = control.settings()
        assert s["enabled"] is False and s["updated_ts"] > 0

    def test_enable_disable(self, control: ControlStore):
        assert control.set_enabled(True)["enabled"] is True
        assert control.set_enabled(False)["enabled"] is False

    def test_enabling_clears_override(self, control: ControlStore):
        control.trip_override("someone touched it")
        assert control.settings()["enabled"] is False
        assert control.state()["override"] == "someone touched it"
        control.set_enabled(True)
        assert control.state()["override"] is None
        assert control.state()["override_ts"] is None

    def test_disabling_does_not_clear_override(self, control: ControlStore):
        control.trip_override("x")
        control.set_enabled(False)
        assert control.state()["override"] == "x"


class TestZones:
    def test_unknown_zone_gets_defaults(self, control: ControlStore):
        assert control.zone("zone:9") == ZONE_DEFAULTS

    def test_update_persists_and_merges(self, control: ControlStore):
        control.update_zone("zone:1", day_d=72, day_hi=74)
        z = control.zone("zone:1")
        assert (z["day_lo"], z["day_d"], z["day_hi"]) == (69, 72, 74)
        assert z["night_d"] == ZONE_DEFAULTS["night_d"]  # untouched
        control.update_zone("zone:1", night_start="21:00")
        z = control.zone("zone:1")
        assert z["night_start"] == "21:00" and z["day_d"] == 72  # earlier edit kept

    def test_zones_bulk(self, control: ControlStore):
        control.update_zone("zone:2", day_lo=60, day_d=62, day_hi=64)
        out = control.zones(["zone:1", "zone:2"])
        assert out["zone:1"] == ZONE_DEFAULTS and out["zone:2"]["day_d"] == 62

    def test_update_bumps_settings_timestamp(self, control: ControlStore):
        before = control.settings()["updated_ts"]
        time.sleep(0.01)
        control.update_zone("zone:1", day_d=70)
        assert control.settings()["updated_ts"] > before

    @pytest.mark.parametrize("fields,msg", [
        ({"day_d": 72}, "day desired temp must be within"),
        ({"day_d": 68}, "day desired temp must be within"),
        ({"night_lo": 70}, "night range must be at least"),
        ({"night_hi": 70.5}, "night range must be at least"),
        ({"day_lo": 70, "day_hi": 71.9}, "day range must be at least"),
    ])
    def test_constraints_rejected(self, control: ControlStore, fields, msg):
        with pytest.raises(ValueError, match=msg):
            control.update_zone("zone:1", **fields)
        assert control.zone("zone:1") == ZONE_DEFAULTS  # nothing written

    def test_min_width_exactly_is_allowed(self, control: ControlStore):
        control.update_zone("zone:1", day_lo=70, day_d=70, day_hi=70 + MIN_WIDTH)
        assert control.zone("zone:1")["day_hi"] == 70 + MIN_WIDTH

    def test_desired_may_sit_on_an_edge(self, control: ControlStore):
        control.update_zone("zone:1", day_lo=68, day_d=68, day_hi=72)
        control.update_zone("zone:1", day_d=72)
        assert control.zone("zone:1")["day_d"] == 72

    def test_unknown_and_none_fields_are_ignored(self, control: ControlStore):
        control.update_zone("zone:1", bogus=1, day_d=None)
        assert control.zone("zone:1") == ZONE_DEFAULTS


class TestState:
    def test_state_defaults(self, control: ControlStore):
        st = control.state()
        assert st["mode"] is None and st["expected"] is None and st["last_eval"] is None
        assert st["dry_run"] is None and st["lean_side"] is None

    def test_set_state_roundtrips_json_fields(self, control: ControlStore):
        control.set_state(expected={"mode": "heat", "zones": {"zone:1": {"htsp": 70}}}, last_eval={"oat": 71.5},
                          mode="heat", lean_side="above", lean_since=123.0)
        st = control.state()
        assert st["expected"]["zones"]["zone:1"]["htsp"] == 70
        assert st["last_eval"] == {"oat": 71.5}
        assert (st["mode"], st["lean_side"], st["lean_since"]) == ("heat", "above", 123.0)

    def test_set_state_with_nothing_is_a_noop(self, control: ControlStore):
        control.set_state()
        assert control.state()["mode"] is None

    def test_heartbeat(self, control: ControlStore):
        control.heartbeat(dry_run=True)
        st = control.state()
        assert st["dry_run"] is True and time.time() - st["loop_alive_ts"] < 5

    def test_trip_override_forgets_expectations(self, control: ControlStore):
        control.set_state(expected={"mode": "cool", "zones": {}}, written_ts=1.0)
        control.trip_override("mode is auto")
        st = control.state()
        assert st["expected"] is None and st["written_ts"] is None
        assert st["override"] == "mode is auto" and st["override_ts"] is not None


class TestLog:
    def test_log_newest_first_with_detail(self, control: ControlStore):
        control.log("a", "first")
        control.log("b", "second", {"k": 1})
        rows = control.recent_log(10)
        assert [r["event"] for r in rows] == ["b", "a"]
        assert rows[0]["detail"] == {"k": 1} and rows[1]["detail"] is None

    def test_recent_log_limit(self, control: ControlStore):
        for i in range(5):
            control.log("e", str(i))
        assert [r["message"] for r in control.recent_log(2)] == ["4", "3"]

    def test_prune_log(self, control: ControlStore):
        control.log("old", "x")
        control.conn.execute("UPDATE control_log SET ts = ?", (time.time() - 40 * 86400,))
        control.log("new", "y")
        control.prune_log(keep_days=30)
        assert [r["event"] for r in control.recent_log()] == ["new"]


class TestMigrations:
    def _legacy_db(self, path: Path) -> None:
        """Schema from the single-target / day-night era, before per-zone rows."""
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE control_settings (id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL DEFAULT 0,
                target REAL NOT NULL DEFAULT 70, updated_ts REAL NOT NULL,
                target_day REAL NOT NULL DEFAULT 70, target_night REAL NOT NULL DEFAULT 70,
                day_start TEXT NOT NULL DEFAULT '07:00', night_start TEXT NOT NULL DEFAULT '22:00');
            INSERT INTO control_settings(id, enabled, target, updated_ts, target_day, target_night, day_start, night_start)
                VALUES (1, 1, 70, 1.0, 72, 66, '06:30', '21:00');
            CREATE TABLE control_state (id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT, mode_since REAL, rule TEXT,
                expected TEXT, written_ts REAL, applied_settings_ts REAL, last_decision_mode TEXT, last_eval_ts REAL,
                last_eval TEXT, override TEXT, override_ts REAL, dry_run INTEGER, loop_alive_ts REAL);
            INSERT INTO control_state(id, mode) VALUES (1, 'cool');
        """)
        conn.commit(); conn.close()

    def test_legacy_globals_become_zone_defaults(self, tmp_path: Path):
        path = tmp_path / "legacy.sqlite"
        self._legacy_db(path)
        cs = ControlStore(path)
        z = cs.zone("zone:1")
        assert (z["day_lo"], z["day_d"], z["day_hi"]) == (71, 72, 73)
        assert (z["night_lo"], z["night_d"], z["night_hi"]) == (65, 66, 67)
        assert (z["day_start"], z["night_start"]) == ("06:30", "21:00")
        assert cs.settings()["enabled"] is True          # kept
        assert cs.state()["mode"] == "cool"              # kept
        assert cs.state()["lean_side"] is None           # column added

    def test_legacy_zone_rows_get_a_desired_temp(self, tmp_path: Path):
        path = tmp_path / "ranges.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE control_zones (entity TEXT PRIMARY KEY, day_lo REAL NOT NULL, day_hi REAL NOT NULL,
                night_lo REAL NOT NULL, night_hi REAL NOT NULL, day_start TEXT NOT NULL, night_start TEXT NOT NULL);
            INSERT INTO control_zones VALUES ('zone:3', 66, 74, 60, 64, '07:00', '22:00');
        """)
        conn.commit(); conn.close()
        cs = ControlStore(path)
        z = cs.zone("zone:3")
        assert (z["day_d"], z["night_d"]) == (70, 62)   # midpoints
        assert set(ZONE_FIELDS) <= set(z)

    def test_reopening_is_idempotent(self, tmp_path: Path):
        path = tmp_path / "c.sqlite"
        ControlStore(path).update_zone("zone:1", day_d=71)
        ControlStore(path)
        assert ControlStore(path).zone("zone:1")["day_d"] == 71

    def test_two_connections_share_the_file(self, tmp_path: Path):
        path = tmp_path / "c.sqlite"
        web, loop = ControlStore(path), ControlStore(path)
        web.set_enabled(True)
        assert loop.settings()["enabled"] is True
        loop.set_state(mode="heat")
        assert web.state()["mode"] == "heat"
