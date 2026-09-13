"""Pure decision logic: lean(), decide(), period_now()."""

from __future__ import annotations

from datetime import datetime

import pytest

from carriermon.control import MARGIN, PERSIST, Decision, ZoneEval, decide, lean, period_now


def Z(name: str, rt: float | None, lo: float = 69, d: float = 70, hi: float = 71) -> ZoneEval:
    return ZoneEval(name, rt, lo, d, hi)


# ---------------------------------------------------------------- lean
class TestLean:
    def test_all_strictly_above(self):
        assert lean([Z("a", 71), Z("b", 72)]) == "above"

    def test_all_strictly_below(self):
        assert lean([Z("a", 69), Z("b", 68)]) == "below"

    def test_one_zone_exactly_at_desired_blocks_the_lean(self):
        # This is the hysteresis: the zone just brought to D reads D.
        assert lean([Z("a", 70), Z("b", 72)]) is None
        assert lean([Z("a", 70), Z("b", 68)]) is None

    def test_mixed_sides_is_no_lean(self):
        assert lean([Z("a", 69), Z("b", 71)]) is None

    def test_per_zone_desired_temps(self):
        assert lean([Z("warm", 73, 71, 72, 73), Z("cool", 69, 67, 68, 69)]) == "above"
        assert lean([Z("warm", 71, 71, 72, 73), Z("cool", 67, 67, 68, 69)]) == "below"

    def test_unknown_readings_are_ignored(self):
        assert lean([Z("a", None), Z("b", 71)]) == "above"

    def test_no_readings_is_no_lean(self):
        assert lean([Z("a", None)]) is None
        assert lean([]) is None


# ---------------------------------------------------------------- decide: tier 1
class TestTier1:
    def test_zone_below_its_range_heats(self):
        d = decide([Z("Boys", 68), Z("1st", 71)], 74)
        assert d.mode == "heat" and "Boys" in d.rule and "below" in d.rule

    def test_zone_above_its_range_cools_even_when_cold_outside(self):
        d = decide([Z("Boys", 70), Z("1st", 72)], 40)
        assert d.mode == "cool" and "1st" in d.rule

    def test_errors_are_measured_from_the_range_edge(self):
        d = decide([Z("a", 73), Z("b", 68)], 60)
        assert (d.hot, d.cold) == (2.0, 1.0)

    def test_both_sides_larger_error_wins_hot(self):
        d = decide([Z("a", 73), Z("b", 68)], 60)
        assert d.mode == "cool" and "larger error is hot" in d.rule

    def test_both_sides_larger_error_wins_cold(self):
        d = decide([Z("a", 72), Z("b", 67)], 90)
        assert d.mode == "heat" and "larger error is cold" in d.rule

    def test_both_sides_equal_outdoor_above_every_zone_cools(self):
        assert decide([Z("a", 72), Z("b", 68)], 80).mode == "cool"

    def test_both_sides_equal_outdoor_below_every_zone_heats(self):
        assert decide([Z("a", 72), Z("b", 68)], 50).mode == "heat"

    def test_both_sides_equal_outdoor_among_zones_keeps(self):
        d = decide([Z("a", 72), Z("b", 68)], 70)
        assert d.mode is None and "keep" in d.rule

    def test_outdoor_tiebreak_needs_the_margin(self):
        # max zone is 72: outdoor must be > 72 + MARGIN to count as "above every zone".
        assert decide([Z("a", 72), Z("b", 68)], 72 + MARGIN).mode is None
        assert decide([Z("a", 72), Z("b", 68)], 72 + MARGIN + 0.5).mode == "cool"
        assert decide([Z("a", 72), Z("b", 68)], 68 - MARGIN).mode is None
        assert decide([Z("a", 72), Z("b", 68)], 68 - MARGIN - 0.5).mode == "heat"

    def test_both_sides_equal_no_outdoor_reading_keeps(self):
        assert decide([Z("a", 72), Z("b", 68)], None).mode is None

    def test_tier1_beats_any_lean(self):
        d = decide([Z("a", 68), Z("b", 69)], 74, lean_side="below", lean_for=PERSIST * 10)
        assert d.mode == "heat" and "cold zone" in d.rule

    def test_per_zone_ranges(self):
        # Nursery wants 71-73, 1st Floor 67-69; both read 70 -> both out by 1 -> tie -> outdoor.
        zones = [Z("Nursery", 70, 71, 72, 73), Z("1st", 70, 67, 68, 69)]
        assert decide(zones, 85).mode == "cool"
        assert decide(zones, 55).mode == "heat"
        assert decide(zones, 70).mode is None


# ---------------------------------------------------------------- decide: tier 2 & 3
class TestTier2And3:
    def test_all_tolerable_no_lean_keeps_regardless_of_outdoor(self):
        for oat in (40, 70, 95, None):
            d = decide([Z("a", 70), Z("b", 71)], oat)
            assert d.mode is None, oat
            assert "keep" in d.rule

    def test_lean_above_needs_to_persist(self):
        zones = [Z("a", 71), Z("b", 71)]
        assert decide(zones, 74, "above", PERSIST - 1).mode is None
        d = decide(zones, 74, "above", PERSIST)
        assert d.mode == "cool" and "above its desired" in d.rule

    def test_lean_below_needs_to_persist(self):
        zones = [Z("a", 69), Z("b", 69)]
        assert decide(zones, 74, "below", 0).mode is None
        assert decide(zones, 74, "below", PERSIST).mode == "heat"

    def test_pending_lean_is_mentioned_in_the_rule_text(self):
        d = decide([Z("a", 71)], 74, "above", 180)
        assert d.mode is None and "above its desired temp for 3 min" in d.rule

    def test_no_zone_readings(self):
        d = decide([Z("a", None)], 74)
        assert d == Decision(None, "no zone temperatures available")

    def test_wide_range_zone_at_edge_is_still_tolerable(self):
        d = decide([Z("a", 74, 66, 70, 74), Z("b", 66, 66, 70, 74)], 90)
        assert d.mode is None


# ---------------------------------------------------------------- period_now
@pytest.mark.parametrize("day_start,night_start,hhmm,expected", [
    ("07:00", "22:00", "06:59", "night"),
    ("07:00", "22:00", "07:00", "day"),
    ("07:00", "22:00", "12:00", "day"),
    ("07:00", "22:00", "21:59", "day"),
    ("07:00", "22:00", "22:00", "night"),
    ("07:00", "22:00", "23:30", "night"),
    ("07:00", "22:00", "00:00", "night"),
    # night-shift schedule: day starts in the evening and wraps midnight
    ("22:00", "06:00", "23:00", "day"),
    ("22:00", "06:00", "03:00", "day"),
    ("22:00", "06:00", "06:00", "night"),
    ("22:00", "06:00", "12:00", "night"),
    # equal times: never day
    ("08:00", "08:00", "08:00", "night"),
])
def test_period_now(day_start, night_start, hhmm, expected):
    h, m = map(int, hhmm.split(":"))
    assert period_now(day_start, night_start, datetime(2026, 9, 13, h, m)) == expected


def test_period_now_defaults_to_wall_clock():
    assert period_now("00:00", "23:59") in ("day", "night")  # "day" except during the 23:59 minute
    assert period_now("12:00", "12:00") == "night"            # zero-length day
