"""mismatches(), describe_writes(), the dry-run and Carrier appliers, read_live()."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from carriermon.control import (CarrierApplier, DryRunApplier, Live, _fmt, describe_writes, mismatches,
                                read_live)

from conftest import populate_readings

DESIRED = {"mode": "cool", "zones": {
    "zone:1": {"name": "Boys", "htsp": 68.0, "clsp": 70.0},
    "zone:2": {"name": "1st", "htsp": 66.0, "clsp": 68.0},
}}


def live(mode="cool", **zone_overrides) -> Live:
    zones = [
        {"entity": "zone:1", "id": "1", "name": "Boys", "rt": 70.0, "htsp": 68.0, "clsp": 70.0, "hold": "on"},
        {"entity": "zone:2", "id": "2", "name": "1st", "rt": 70.0, "htsp": 66.0, "clsp": 68.0, "hold": "on"},
    ]
    for z in zones:
        z.update(zone_overrides.get(z["entity"], {}))
    return Live(mode=mode, oat=70.0, gap=2.0, zones=zones)


class TestMismatches:
    def test_match(self):
        assert mismatches(live(), DESIRED) == []

    def test_mode(self):
        assert mismatches(live(mode="heat"), DESIRED) == ["mode is heat, expected cool"]

    def test_setpoints_and_hold(self):
        out = mismatches(live(**{"zone:1": {"htsp": 69.0, "hold": "off"}, "zone:2": {"clsp": 69.5}}), DESIRED)
        assert out == ["Boys heat setpoint is 69.0, expected 68", "Boys hold is off, expected on",
                       "1st cool setpoint is 69.5, expected 68"]

    def test_float_noise_tolerated(self):
        assert mismatches(live(**{"zone:1": {"htsp": 68.001}}), DESIRED) == []

    def test_non_numeric_live_value(self):
        assert mismatches(live(**{"zone:1": {"clsp": "70"}}), DESIRED) == ["Boys cool setpoint is 70, expected 70"]

    def test_zone_missing_from_live_is_skipped(self):
        lv = live()
        lv.zones = lv.zones[:1]
        assert mismatches(lv, DESIRED) == []


class TestDescribeWrites:
    def test_first_write_lists_everything(self):
        assert describe_writes(DESIRED, None) == [
            "Boys: heat 68 / cool 70, hold on", "1st: heat 66 / cool 68, hold on", "mode cool"]

    def test_only_changed_zones_and_mode(self):
        prev = {"mode": "cool", "zones": {**DESIRED["zones"], "zone:2": {"name": "1st", "htsp": 60.0, "clsp": 62.0}}}
        assert describe_writes(DESIRED, prev) == ["1st: heat 66 / cool 68, hold on"]
        prev = {"mode": "heat", "zones": DESIRED["zones"]}
        assert describe_writes(DESIRED, prev) == ["mode cool"]

    def test_nothing_changed(self):
        assert describe_writes(DESIRED, DESIRED) == []


def test_fmt():
    assert _fmt(70.0) == "70" and _fmt(70) == "70" and _fmt(70.5) == "70.5"


class TestDryRunApplier:
    def test_describes_without_side_effects(self):
        out = asyncio.run(DryRunApplier().apply("S", DESIRED, None))
        assert out == ["would write: Boys: heat 68 / cool 70, hold on", "would write: 1st: heat 66 / cool 68, hold on",
                       "would write: mode cool"]
        assert DryRunApplier.dry_run is True


class FakeApi:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_at = fail_at

    async def _rec(self, *call):
        self.calls.append(call)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ConnectionError("504")

    async def set_config_activity(self, serial, zone_id, activity, htsp, clsp):
        await self._rec("activity", serial, zone_id, activity.value, htsp, clsp)

    async def set_config_hold(self, serial, zone_id, activity, until):
        await self._rec("hold", serial, zone_id, activity.value, until)

    async def set_config_mode(self, serial, mode):
        await self._rec("mode", serial, mode.value)


class TestCarrierApplier:
    def test_zones_then_hold_then_mode(self):
        api = FakeApi()
        out = asyncio.run(CarrierApplier(api).apply("S", DESIRED, None))
        assert api.calls == [
            ("activity", "S", "1", "manual", "68", "70"), ("hold", "S", "1", "manual", None),
            ("activity", "S", "2", "manual", "66", "68"), ("hold", "S", "2", "manual", None),
            ("mode", "S", "cool"),
        ]
        assert out == ["Boys: heat 68 / cool 70, hold on", "1st: heat 66 / cool 68, hold on", "mode cool"]

    def test_skips_unchanged_zones_and_mode(self):
        api = FakeApi()
        prev = {"mode": "cool", "zones": {**DESIRED["zones"], "zone:1": {"name": "Boys", "htsp": 60.0, "clsp": 62.0}}}
        out = asyncio.run(CarrierApplier(api).apply("S", DESIRED, prev))
        assert [c[0] for c in api.calls] == ["activity", "hold"] and api.calls[0][2] == "1"
        assert out == ["Boys: heat 68 / cool 70, hold on"]

    def test_fractional_setpoints_are_formatted(self):
        api = FakeApi()
        d = {"mode": "heat", "zones": {"zone:1": {"name": "Boys", "htsp": 69.5, "clsp": 71.5}}}
        asyncio.run(CarrierApplier(api).apply("S", d, None))
        assert api.calls[0][4:] == ("69.5", "71.5")

    def test_error_propagates_after_partial_writes(self):
        api = FakeApi(fail_at=3)
        with pytest.raises(ConnectionError):
            asyncio.run(CarrierApplier(api).apply("S", DESIRED, None))
        assert len(api.calls) == 3  # zone 1 fully written, zone 2 not
        assert CarrierApplier.dry_run is False


class TestReadLive:
    def test_reads_latest_values_and_enabled_zones(self, tmp_path: Path):
        store = populate_readings(tmp_path / "r.sqlite")
        lv = read_live(store, "S1")
        assert lv.mode == "cool" and lv.oat == 80.0 and lv.gap == 2.0
        assert [z["name"] for z in lv.zones] == ["Upstairs", "Downstairs"]
        up = lv.zones[0]
        assert (up["entity"], up["id"], up["rt"], up["htsp"], up["clsp"], up["hold"]) == ("zone:1", "1", 72.0, 69.0, 71.0, "off")
        assert lv.zones[1]["rt"] == 68.0 and lv.zones[1]["htsp"] is None

    def test_missing_deadband_falls_back_to_default(self, tmp_path: Path):
        store = populate_readings(tmp_path / "r.sqlite")
        store.conn.execute("DELETE FROM readings WHERE field='cfgdead'"); store.conn.commit()
        assert read_live(store, "S1").gap == 2.0
