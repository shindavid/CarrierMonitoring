"""db.Store: the readings database queries the controller and dashboard rely on."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from carriermon.db import Store

from conftest import populate_readings

T0 = 1_000_000.0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return populate_readings(tmp_path / "r.sqlite")


class TestLatest:
    def test_numeric_and_text(self, store: Store):
        assert store.latest("S1", "zone:1", "rt") == 72.0
        assert store.latest("S1", "system", "mode") == "cool"
        assert store.latest("S1", "zone:1", "hold") == "off"

    def test_unknown(self, store: Store):
        assert store.latest("S1", "zone:1", "nope") is None
        assert store.latest("nope", "zone:1", "rt") is None


class TestZones:
    def test_enabled_zones_with_names_in_id_order(self, store: Store):
        assert store.zones("S1") == [{"entity": "zone:1", "name": "Upstairs"}, {"entity": "zone:2", "name": "Downstairs"}]

    def test_name_falls_back_to_entity(self, store: Store):
        store.add_readings([(T0, "S1", "zone:5", "enabled", None, "on", 1, "cloud:load")])
        assert {"entity": "zone:5", "name": "zone:5"} in store.zones("S1")

    def test_latest_name_wins(self, store: Store):
        store.add_readings([(T0 + 500, "S1", "config.zone:1", "name", None, "Loft", 1, "cloud:ws")])
        assert store.zones("S1")[0]["name"] == "Loft"


class TestSeriesAndEvents:
    def test_series_includes_prior_value_for_step_charts(self, store: Store):
        rows = store.series("S1", "zone:1", "rt", T0 + 100, T0 + 200)
        assert [r["value_num"] for r in rows] == [71.0, 71.0, 72.0]
        assert rows[0]["ts"] == T0 + 60  # last value before the window

    def test_series_without_prior(self, store: Store):
        rows = store.series("S1", "zone:1", "rt", T0 - 10, T0 + 10)
        assert [r["value_num"] for r in rows] == [70.0]

    def test_events_are_changes_only_newest_first(self, store: Store):
        ev = store.events("S1", T0 + 1, T0 + 300)
        assert [(e["entity"], e["field"]) for e in ev] == [("system", "mode"), ("zone:1", "rt"), ("zone:1", "rt")]
        assert all(e["source"] for e in ev)

    def test_events_limit_and_any_serial(self, store: Store):
        assert len(store.events(None, 0, T0 + 300, limit=2)) == 2

    def test_serials_and_fields(self, store: Store):
        assert store.serials() == ["S1"]
        rt = next(f for f in store.fields() if f["entity"] == "zone:1" and f["field"] == "rt")
        assert rt["rows_total"] == 4 and rt["numeric_rows"] == 4 and rt["first_ts"] == T0


class TestReadOnlyAndPrune:
    def test_read_only_connection_cannot_write(self, tmp_path: Path):
        path = tmp_path / "r.sqlite"
        populate_readings(path)
        ro = Store(path, read_only=True)
        with pytest.raises(Exception):
            ro.add_readings([(T0, "S1", "zone:1", "rt", 1.0, None, 1, "x")])

    def test_read_only_requires_existing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            Store(tmp_path / "missing.sqlite", read_only=True)

    def test_prune_keeps_config_rows(self, store: Store):
        store.add_raw("cloud:poll", "S1", {"old": True}, ts=T0)
        deleted = store.prune(retention_days=1, vacuum=False)
        assert deleted > 0
        assert store.latest("S1", "zone:1", "rt") is None           # telemetry gone
        assert store.latest("S1", "config.zone:1", "name") == "Upstairs"  # config kept
        assert store.conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == 0

    def test_prune_nothing_recent(self, tmp_path: Path):
        s = Store(tmp_path / "n.sqlite")
        s.add_readings([(time.time(), "S", "zone:1", "rt", 70.0, None, 1, "x")])
        assert s.prune(retention_days=7) == 0
        assert s.latest("S", "zone:1", "rt") == 70.0

    def test_last_values(self, store: Store):
        lv = store.last_values("S1")
        assert lv[("zone:1", "rt")] == 72.0 and lv[("system", "mode")] == "cool"
