"""bus: decoding infinitesp's JSONL frames and change-tracking the zone readings.

Frames are real captures from a SYSTXCC4ZC01 zone controller and three
SYSTXZNSMS01 Smart Sensors."""

from __future__ import annotations

import json
import time

import pytest

from carriermon.bus import BusIngest, decode, report, zone_of
from carriermon.db import Store
from carriermon.settings import parse_zone_names

ZC_0302 = {"src": "60", "dst": "20", "func": "06", "reg": "0302",
           "data": "010104A804020000040300000404000004140000041C0000"}
SENSOR_22 = {"src": "22", "dst": "20", "func": "06", "reg": "041E",
             "data": "800000000000454801045B453B00000000000000"}


def line(frame: dict, **changes) -> bytes:
    return json.dumps({**frame, **changes}).encode() + b"\n"


class TestDecode:
    def test_zone_controller_reports_only_present_sensors(self):
        # Zone 1 has a remote sensor on ZS1 (0x04A8 / 16); zones 2-4 are "not installed".
        assert decode(ZC_0302) == {("bus.zone:1", "rt"): 74.5}

    def test_second_zone_controller_covers_zones_5_to_8(self):
        assert decode({**ZC_0302, "src": "61"}) == {("bus.zone:5", "rt"): 74.5}

    def test_smart_sensor_by_address(self):
        assert decode(SENSOR_22) == {("bus.sensor:22", "rt"): 0x045B / 16}

    def test_ignores_the_thermostats_own_writes_and_reads(self):
        write = {"src": "20", "dst": "22", "func": "0C", "reg": "041F",
                 "data": "8000000000004548000004FFF000000000000000"}
        assert decode(write) == {}
        assert decode({"src": "20", "dst": "60", "func": "0B", "reg": "0302", "data": ""}) == {}

    def test_garbage_and_implausible_values_dropped(self):
        assert decode({**ZC_0302, "data": "zz"}) == {}
        assert decode({**ZC_0302, "data": ZC_0302["data"][:20]}) == {}       # truncated
        assert decode({**SENSOR_22, "data": "8000000000004548010000453B"}) == {}  # 0 °F


class TestBusIngest:
    def test_records_changes_only_then_anchors(self, settings, tmp_path):
        store = Store(settings.db_path)
        bus = BusIngest(settings, store, "SER")
        t = bus.last_anchor
        assert bus.handle_line(line(ZC_0302), now=t + 1) == 1
        assert bus.handle_line(line(ZC_0302), now=t + 2) == 0            # unchanged
        assert bus.handle_line(b"not json\n", now=t + 3) == 0
        assert bus.handle_line(line(SENSOR_22), now=t + 4) == 1
        # Once per poll interval every current value is re-recorded, unchanged ones flagged so.
        assert bus.handle_line(line(SENSOR_22), now=t + settings.poll_seconds) == 2
        rows = store.conn.execute("SELECT entity, value_num, changed, source FROM readings ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [
            ("bus.zone:1", 74.5, 1, "bus:tap"),
            ("bus.sensor:22", 0x045B / 16, 1, "bus:tap"),
            ("bus.zone:1", 74.5, 0, "bus:tap"),
            ("bus.sensor:22", 0x045B / 16, 0, "bus:tap"),
        ]

    def test_change_on_an_anchor_tick_is_still_flagged_changed(self, settings):
        store = Store(settings.db_path)
        bus = BusIngest(settings, store, "SER")
        t = bus.last_anchor
        bus.handle_line(line(ZC_0302), now=t + 1)
        warmer = ZC_0302["data"].replace("04A8", "04B0", 1)
        bus.handle_line(line(ZC_0302, data=warmer), now=t + settings.poll_seconds)
        assert tuple(store.conn.execute(
            "SELECT value_num, changed FROM readings ORDER BY id DESC LIMIT 1").fetchone()) == (75.0, 1)

    def test_reconnect_forgets_pre_outage_values(self, settings):
        # An anchor right after an outage must not re-record the last pre-outage value.
        store = Store(settings.db_path)
        bus = BusIngest(settings, store, "SER")
        t = bus.last_anchor
        bus.handle_line(line(ZC_0302), now=t + 1)
        bus.on_connect()
        bus.handle_line(line(SENSOR_22), now=t + 2 * settings.poll_seconds)
        rows = store.conn.execute("SELECT entity, changed FROM readings ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("bus.zone:1", 1), ("bus.sensor:22", 1)]

    def test_resumes_change_tracking_from_stored_values(self, settings):
        store = Store(settings.db_path)
        BusIngest(settings, store, "SER").handle_line(line(ZC_0302))
        assert BusIngest(settings, store, "SER").handle_line(line(ZC_0302)) == 0


class TestReport:
    def test_zone_of(self):
        assert zone_of("bus.zone:1") == 1 and zone_of("bus.sensor:23") == 3
        assert zone_of("bus.sensor:zz") is None and zone_of("zone:1") is None

    def test_latest_bus_value_beside_cloud(self, settings):
        store = Store(settings.db_path)
        bus = BusIngest(settings, store, "SER")
        bus.handle_line(line(ZC_0302), now=1000.0)
        bus.handle_line(line(SENSOR_22), now=1010.0)
        store.add_readings([(1000.0, "SER", "zone:1", "rt", 74.0, None, 1, "cloud:poll"),
                            (1000.0, "SER", "config.zone:1", "name", None, "2nd Floor", 1, "cloud:poll")])
        out = report(store, minutes=1, now=1020.0).splitlines()
        assert out[1].split() == ["1", "2nd", "Floor", "bus.zone:1", "74.50", "20s", "74"]
        assert out[2].split() == ["2", "bus.sensor:22", "69.69", "10s"]      # no cloud zone:2 yet
        assert "  " + time.strftime("%m-%d %H:%M:%S", time.localtime(1010.0)) + "  69.69" in out

    def test_zone_names_from_env_win_over_cloud(self, settings):
        store = Store(settings.db_path)
        BusIngest(settings, store, "SER").handle_line(line(ZC_0302), now=1000.0)
        store.add_readings([(1000.0, "SER", "config.zone:1", "name", None, "Upstairs", 1, "cloud:poll")])
        assert "Upstairs" in report(store, now=1001.0)
        assert "Attic" in report(store, now=1001.0, zone_names={1: "Attic"})


def test_parse_zone_names():
    assert parse_zone_names(" 1=2nd Floor, 4=Boys room ,") == {1: "2nd Floor", 4: "Boys room"}
    assert parse_zone_names("") == {}
    with pytest.raises(SystemExit):
        parse_zone_names("upstairs")
