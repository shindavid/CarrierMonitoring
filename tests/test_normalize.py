"""normalize: flattening Carrier payloads and change-tracking rows."""

from __future__ import annotations

from carriermon.normalize import _coerce, diff_rows, flatten, snapshot


class TestCoerce:
    def test_numbers_and_numeric_strings(self):
        assert _coerce(70) == (70.0, None)
        assert _coerce("70") == (70.0, None)
        assert _coerce(" 0.58 ") == (0.58, None)

    def test_text_bools_none(self):
        assert _coerce("cool") == (None, "cool")
        assert _coerce(True) == (None, "on") and _coerce(False) == (None, "off")
        assert _coerce(None) == (None, None)
        assert _coerce([1]) == (None, "[1]")


class TestFlatten:
    def test_nested_and_skipped_keys(self):
        out = flatten({"a": {"b": 1, "etag": "x"}, "c": "d", "__typename": "T"}, "system")
        assert out == {("system", "a.b"): 1, ("system", "c"): "d"}

    def test_lists_of_ids_become_entities(self):
        out = flatten({"zones": [{"id": "1", "rt": "70"}, {"id": "2", "rt": "68"}]}, "system")
        assert out == {("zone:1", "rt"): "70", ("zone:2", "rt"): "68"}

    def test_lists_under_a_sub_entity_are_prefixed(self):
        out = flatten({"activities": [{"id": "home", "htsp": "70"}]}, "config.zone:1")
        assert out == {("config.zone:1.activity:home", "htsp"): "70"}

    def test_plain_lists_are_indexed(self):
        out = flatten({"days": ["mon", "tue"]}, "config")
        assert out == {("config", "days[0]"): "mon", ("config", "days[1]"): "tue"}


class TestSnapshot:
    def test_units_and_config_get_their_own_entities(self):
        status = {"mode": "cool", "idu": {"cfm": "500"}, "odu": {"opstat": "Stage 1"}, "zones": [{"id": "1", "rt": "70"}]}
        out = snapshot(status, {"cfgdead": "2", "zones": [{"id": "1", "name": "Boys"}]})
        assert out[("system", "mode")] == "cool" and out[("idu", "cfm")] == "500" and out[("odu", "opstat")] == "Stage 1"
        assert out[("zone:1", "rt")] == "70" and out[("config", "cfgdead")] == "2"
        assert out[("config.zone:1", "name")] == "Boys"

    def test_snapshot_does_not_mutate_input(self):
        status = {"idu": {"cfm": "1"}, "mode": "heat"}
        snapshot(status, None)
        assert "idu" in status


class TestDiffRows:
    def test_first_sight_is_a_change(self):
        last: dict = {}
        rows, changed = diff_rows("S", {("zone:1", "rt"): "70"}, last, "cloud:ws", force_all=False, ts=1.0)
        assert rows == [(1.0, "S", "zone:1", "rt", 70.0, None, 1, "cloud:ws")] and changed == 1
        assert last[("zone:1", "rt")] == 70.0

    def test_unchanged_values_are_dropped_unless_forced(self):
        last = {("zone:1", "rt"): 70.0, ("config", "cfgdead"): 2.0}
        values = {("zone:1", "rt"): "70", ("config", "cfgdead"): "2"}
        rows, changed = diff_rows("S", values, last, "cloud:ws", force_all=False, ts=1.0)
        assert rows == [] and changed == 0
        rows, changed = diff_rows("S", values, last, "cloud:poll", force_all=True, ts=2.0)
        # anchor for telemetry, none for config
        assert [(r[2], r[6]) for r in rows] == [("zone:1", 0)] and changed == 0

    def test_text_change(self):
        last = {("system", "mode"): "cool"}
        rows, changed = diff_rows("S", {("system", "mode"): "heat"}, last, "cloud:ws", False, ts=3.0)
        assert rows[0][5] == "heat" and rows[0][6] == 1 and changed == 1

    def test_timestamp_defaults_to_now(self):
        import time
        rows, _ = diff_rows("S", {("a", "b"): 1}, {}, "x", False)
        assert abs(rows[0][0] - time.time()) < 5
