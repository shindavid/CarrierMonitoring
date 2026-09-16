"""ControlLoop.tick(): the glue between live state, rules, writes and overrides."""

from __future__ import annotations

import time

from carriermon.control import MAX_WRITE_ATTEMPTS, PERSIST, ControlLoop
from carriermon.controldb import ControlStore

from conftest import FakeStore, RecApplier, make_settings, tick


def events(control: ControlStore, *kinds: str) -> list[str]:
    return [l["message"] for l in reversed(control.recent_log(200)) if l["event"] in kinds]


class TestOffAndOn:
    def test_disabled_loop_only_heartbeats(self, loop, control, applier):
        tick(loop)
        assert applier.calls == []
        assert control.state()["loop_alive_ts"] is not None
        assert control.state()["last_eval"] is None

    def test_enable_writes_everything_once(self, loop, control, applier, fake_store):
        control.set_enabled(True)
        tick(loop)
        assert len(applier.calls) == 1
        desired = applier.calls[0]
        assert desired["mode"] == "heat"  # thermostat already in heat, all zones tolerable -> keep
        assert desired["zones"]["zone:1"] == {"name": "Boys", "htsp": 70.0, "clsp": 72.0}
        st = control.state()
        assert st["expected"] == desired and st["mode"] == "heat" and st["written_ts"] is not None
        assert "mode → heat" in events(control, "mode")[0]

    def test_steady_state_writes_nothing(self, loop, control, applier):
        control.set_enabled(True)
        tick(loop); tick(loop); tick(loop)
        assert len(applier.calls) == 1
        assert len(events(control, "check")) == 3

    def test_switching_off_forgets_claim_and_leaves_thermostat(self, loop, control, applier):
        control.set_enabled(True)
        tick(loop)
        control.set_enabled(False)
        tick(loop)
        st = control.state()
        assert st["expected"] is None and st["mode"] is None and st["lean_side"] is None
        assert len(applier.calls) == 1
        assert events(control, "disabled") == ["controller switched off; thermostat left as is"]

    def test_starting_from_auto_picks_mode_from_outdoor(self, loop, control, applier, fake_store):
        fake_store.set("system", "mode", "auto")
        fake_store.set("system", "oat", 90.0)
        control.set_enabled(True)
        tick(loop)
        assert applier.calls[0]["mode"] == "cool"
        assert "picking cool from outdoor temp" in control.state()["rule"]

    def test_no_serial(self, settings, control, applier):
        class Empty(FakeStore):
            def serials(self): return []
        lp = ControlLoop(settings, Empty(), control, applier)  # type: ignore[arg-type]
        control.set_enabled(True)
        tick(lp)
        assert control.state()["last_eval"] == {"error": "no system in readings database"}
        assert applier.calls == []


class TestSetpoints:
    def test_cool_mode_puts_cool_setpoint_at_desired(self, loop, control, applier, fake_store):
        control.update_zone("zone:1", day_lo=66, day_d=68, day_hi=72, night_lo=66, night_d=68, night_hi=72)
        fake_store.set_zone("zone:2", rt=73.0)  # above 69-71 -> cool
        control.set_enabled(True)
        tick(loop)
        z = applier.calls[0]["zones"]
        assert applier.calls[0]["mode"] == "cool"
        assert z["zone:1"] == {"name": "Boys", "htsp": 66.0, "clsp": 68.0}
        assert z["zone:2"] == {"name": "1st", "htsp": 68.0, "clsp": 70.0}

    def test_deadband_from_config_or_default(self, loop, control, applier, fake_store):
        fake_store.set("config", "cfgdead", 3.0)
        control.set_enabled(True)
        tick(loop)
        assert applier.calls[0]["zones"]["zone:1"]["clsp"] == 73.0
        fake_store.set("config", "cfgdead", None)
        control.update_zone("zone:1", day_d=71, night_d=71)  # force a rewrite
        tick(loop)
        assert applier.calls[-1]["zones"]["zone:1"] == {"name": "Boys", "htsp": 71.0, "clsp": 73.0}

    def test_changing_a_zone_rewrites_setpoints_not_mode(self, loop, control, applier):
        control.set_enabled(True)
        tick(loop)
        control.update_zone("zone:2", day_d=71, night_d=71)
        tick(loop)
        assert len(applier.calls) == 2
        assert applier.calls[1]["mode"] == "heat"
        assert applier.calls[1]["zones"]["zone:2"]["htsp"] == 71.0
        assert len(events(control, "mode")) == 1

    def test_period_switch_changes_setpoints(self, loop, control, applier):
        # Day and night differ; "now" is day for zone:1 (00:00-23:59) then flipped to night.
        # Night range still contains the current 70, so the mode stays heat and only D moves.
        control.update_zone("zone:1", night_lo=68, night_d=69, night_hi=72, day_start="00:00", night_start="23:59")
        control.set_enabled(True)
        tick(loop)
        assert applier.calls[-1]["zones"]["zone:1"]["htsp"] == 70.0
        assert control.state()["last_eval"]["zones"][0]["period"] == "day"
        control.update_zone("zone:1", day_start="23:58")  # now falls outside [23:58, 23:59)
        tick(loop)
        assert applier.calls[-1]["mode"] == "heat"
        assert applier.calls[-1]["zones"]["zone:1"] == {"name": "Boys", "htsp": 69.0, "clsp": 71.0}
        assert control.state()["last_eval"]["zones"][0]["period"] == "night"

    def test_period_switch_can_change_mode_too(self, loop, control, applier):
        # At night Boys wants 64-68; sitting at 70 it is now above range -> cool.
        control.update_zone("zone:1", night_lo=64, night_d=66, night_hi=68, day_start="23:58", night_start="23:59")
        control.set_enabled(True)
        tick(loop)
        assert applier.calls[-1]["mode"] == "cool"
        assert applier.calls[-1]["zones"]["zone:1"] == {"name": "Boys", "htsp": 64.0, "clsp": 66.0}


class TestLeanTimer:
    def test_zone_at_desired_blocks_lean(self, loop, control, fake_store):
        control.set_enabled(True)
        fake_store.set_zone("zone:2", rt=71.0)  # zone:1 at 70 == D
        tick(loop)
        assert control.state()["lean_side"] is None

    def test_lean_starts_persists_and_switches(self, loop, control, applier, fake_store):
        control.set_enabled(True)
        tick(loop)
        fake_store.set_zone("zone:1", rt=71.0); fake_store.set_zone("zone:2", rt=71.0)
        tick(loop)
        st = control.state()
        assert st["lean_side"] == "above" and st["mode"] == "heat"
        since = st["lean_since"]
        tick(loop)
        assert control.state()["lean_since"] == since  # timer keeps running
        control.set_state(lean_since=time.time() - PERSIST - 1)
        tick(loop)
        assert control.state()["mode"] == "cool"
        assert applier.calls[-1]["mode"] == "cool"
        assert "above its desired temp" in events(control, "mode")[-1]

    def test_lean_resets_when_it_flips_or_disappears(self, loop, control, fake_store):
        control.set_enabled(True)
        fake_store.set_zone("zone:1", rt=71.0); fake_store.set_zone("zone:2", rt=71.0)
        tick(loop)
        control.set_state(lean_since=time.time() - 200)
        fake_store.set_zone("zone:1", rt=69.0); fake_store.set_zone("zone:2", rt=69.0)
        tick(loop)
        st = control.state()
        assert st["lean_side"] == "below" and time.time() - st["lean_since"] < 5
        fake_store.set_zone("zone:1", rt=70.0)
        tick(loop)
        assert control.state()["lean_side"] is None and control.state()["lean_since"] is None

    def test_tier1_ignores_lean_timer(self, loop, control, applier, fake_store):
        control.set_enabled(True)
        fake_store.set_zone("zone:1", rt=68.0)  # below range
        fake_store.set_zone("zone:2", rt=68.0)
        fake_store.set("system", "mode", "cool")
        tick(loop)
        assert applier.calls[-1]["mode"] == "heat"


class TestOverride:
    def test_manual_change_trips_after_grace(self, loop, control, applier, fake_store):
        control.set_enabled(True)
        tick(loop)
        fake_store.set_zone("zone:1", clsp=75.0)  # someone bumped it at the thermostat
        tick(loop)
        assert control.settings()["enabled"] is False
        st = control.state()
        assert st["override"] == "Boys cool setpoint is 75.0, expected 72"
        assert st["expected"] is None and st["mode"] is None
        assert events(control, "override")[-1].startswith("manual change detected")

    def test_mode_and_hold_changes_trip(self, loop, control, fake_store):
        control.set_enabled(True)
        tick(loop)
        fake_store.set("system", "mode", "auto")
        fake_store.set_zone("zone:2", hold="off")
        tick(loop)
        assert control.state()["override"] == "mode is auto, expected heat; 1st hold is off, expected on"

    def test_within_grace_period_no_trip(self, loop, control, fake_store):
        loop.grace = 3600
        control.set_enabled(True)
        tick(loop)
        fake_store.set("system", "mode", "auto")
        tick(loop)
        assert control.settings()["enabled"] is True and control.state()["override"] is None

    def test_tripped_loop_stays_off_until_reenabled(self, loop, control, applier, fake_store):
        control.set_enabled(True)
        tick(loop)
        fake_store.set("system", "mode", "cool")
        tick(loop)
        n = len(applier.calls)
        tick(loop); tick(loop)
        assert len(applier.calls) == n
        control.set_enabled(True)
        tick(loop)
        assert len(applier.calls) == n + 1 and control.state()["override"] is None

    def test_dry_run_reports_but_never_trips(self, settings, control, fake_store):
        applier = RecApplier(dry_run=True)  # no mirror: live never matches
        lp = ControlLoop(settings, fake_store, control, applier, serial="X")  # type: ignore[arg-type]
        lp.grace = 0
        control.set_enabled(True)
        tick(lp)
        fake_store.set("system", "mode", "cool")
        tick(lp); tick(lp)
        assert control.settings()["enabled"] is True
        msgs = events(control, "override")
        assert len(msgs) == 1 and msgs[0].startswith("(dry run) would switch off: mode is cool, expected heat")

    def test_missing_live_setpoint_counts_as_mismatch(self, loop, control, fake_store):
        control.set_enabled(True)
        tick(loop)
        fake_store.set_zone("zone:1", htsp=None)
        tick(loop)
        assert "Boys heat setpoint is None, expected 70" in control.state()["override"]


class TestWriteFailures:
    def test_failed_write_is_retried_not_treated_as_override(self, settings, control, fake_store):
        applier = RecApplier(fail_on={2}, mirror=fake_store)
        lp = ControlLoop(settings, fake_store, control, applier, serial="X")  # type: ignore[arg-type]
        lp.grace = 0
        control.set_enabled(True)
        tick(lp)
        fake_store.set_zone("zone:2", rt=73.0)   # -> cool; this write fails
        tick(lp)
        st = control.state()
        assert st["expected"] is None and control.settings()["enabled"] is True
        assert events(control, "error")[-1].startswith("write failed, will retry next check: RuntimeError: 504")
        tick(lp)                                  # retried and applied
        assert len(applier.calls) == 3 and control.state()["expected"]["mode"] == "cool"
        assert control.state()["override"] is None

    def test_partial_write_before_failure_does_not_trip(self, settings, control, fake_store):
        class HalfApplier(RecApplier):
            async def apply(self, serial, desired, previous):
                self.calls.append(desired)
                if len(self.calls) == 2:
                    first = next(iter(desired["zones"].items()))
                    fake_store.set_zone(first[0], htsp=first[1]["htsp"], clsp=first[1]["clsp"])
                    raise RuntimeError("504")
                fake_store.mirror(desired)
                return ["ok"]
        applier = HalfApplier()
        lp = ControlLoop(settings, fake_store, control, applier, serial="X")  # type: ignore[arg-type]
        lp.grace = 0
        control.set_enabled(True)
        tick(lp)
        fake_store.set_zone("zone:2", rt=73.0)
        tick(lp)   # half-applied, then error
        tick(lp)   # would have tripped on the half-applied zone before the fix
        assert control.settings()["enabled"] is True and control.state()["override"] is None
        assert control.state()["expected"]["mode"] == "cool"

    def test_tick_exception_is_logged_by_run_loop(self, settings, control, fake_store):
        """run() must survive a tick that raises (e.g. the readings DB briefly locked)."""
        import asyncio

        class Boom(FakeStore):
            def serials(self): raise RuntimeError("database is locked")
        lp = ControlLoop(make_settings(settings.db_path.parent, control_interval=0), Boom(), control,
                         RecApplier())  # type: ignore[arg-type]
        control.set_enabled(True)

        async def two_ticks():
            task = asyncio.create_task(lp.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(two_ticks())
        assert any(m.startswith("RuntimeError: database is locked") for m in events(control, "error"))
        assert events(control, "start")


class DropApplier(RecApplier):
    """Mirrors a write except that the thermostat 'ignores' one zone's heat setpoint
    on the listed call numbers, the way Carrier drops a field from a burst."""

    def __init__(self, store: FakeStore, drop_on: set[int], zone: str = "zone:2") -> None:
        super().__init__(mirror=store)
        self.drop_on, self.zone = drop_on, zone

    async def apply(self, serial, desired, previous):
        self.calls.append(desired); self.previous.append(previous)
        drop = len(self.calls) in self.drop_on
        self.mirror.set("system", "mode", desired["mode"])
        for entity, sp in desired["zones"].items():
            fields = {"htsp": sp["htsp"], "clsp": sp["clsp"], "hold": "on"}
            if drop and entity == self.zone:
                del fields["htsp"]           # the thermostat never shows this one
            self.mirror.set_zone(entity, **fields)
        return ["ok"]


class TestDroppedWrites:
    """Carrier's API says OK but the thermostat never shows part of the write."""

    def _loop(self, settings, control, fake_store, applier):
        lp = ControlLoop(settings, fake_store, control, applier, serial="X")  # type: ignore[arg-type]
        lp.grace = 0
        # Thermostat starts away from the target so the first write actually changes it.
        for z in ("zone:1", "zone:2"):
            fake_store.set_zone(z, htsp=66.0, clsp=68.0)
        control.set_enabled(True)
        return lp

    def test_dropped_field_is_rewritten_not_treated_as_override(self, settings, control, fake_store):
        applier = DropApplier(fake_store, drop_on={1})
        lp = self._loop(settings, control, fake_store, applier)
        tick(lp)                                  # write; 1st's heat setpoint stays 66
        assert fake_store.latest("X", "zone:2", "htsp") == 66.0
        tick(lp)                                  # never seen 70 -> retry, not a manual override
        assert control.settings()["enabled"] is True and control.state()["override"] is None
        assert len(applier.calls) == 2 and control.state()["write_attempts"] == 2
        assert events(control, "retry") == [
            f"write not applied (attempt 1 of {MAX_WRITE_ATTEMPTS}): 1st heat setpoint is 66.0, expected 70;"
            " rewriting what is missing"]
        # The retry diffs against what the thermostat shows: Boys is skipped, 1st is sent.
        prev = applier.previous[-1]
        assert prev["mode"] == "heat" and prev["zones"]["zone:1"] == {"name": "Boys", "htsp": 70.0, "clsp": 72.0}
        assert prev["zones"]["zone:2"] == {"name": "1st", "htsp": 66.0, "clsp": 72.0}
        tick(lp)                                  # now it shows
        assert events(control, "write")[-1] == "thermostat now shows what was written (took 2 attempts)"
        assert control.state()["write_attempts"] == 1
        tick(lp)
        assert len(applier.calls) == 2            # quiet again

    def test_gives_up_after_max_attempts_with_an_honest_reason(self, settings, control, fake_store):
        applier = DropApplier(fake_store, drop_on=set(range(1, 10)))
        lp = self._loop(settings, control, fake_store, applier)
        for _ in range(MAX_WRITE_ATTEMPTS):
            tick(lp)
        assert control.settings()["enabled"] is True and len(applier.calls) == MAX_WRITE_ATTEMPTS
        tick(lp)
        assert control.settings()["enabled"] is False
        st = control.state()
        assert st["override"] == (f"thermostat did not apply the controller's settings after {MAX_WRITE_ATTEMPTS}"
                                  " attempts (1st heat setpoint is 66.0, expected 70)")
        assert st["expected"] is None and st["write_attempts"] is None and st["mode"] is None
        assert events(control, "override")[-1].startswith("write never applied, controller switched off")
        assert not any(m.startswith("manual change") for m in events(control, "override"))

    def test_human_change_after_a_dropped_field_still_trips(self, settings, control, fake_store):
        applier = DropApplier(fake_store, drop_on={1})
        lp = self._loop(settings, control, fake_store, applier)
        tick(lp)
        fake_store.set_zone("zone:1", clsp=75.0)  # Boys did take the write, then someone moved it
        tick(lp)
        assert control.settings()["enabled"] is False
        assert control.state()["override"] == "Boys cool setpoint is 75.0, expected 72; 1st heat setpoint is 66.0, expected 70"

    def test_newer_target_supersedes_an_unconfirmed_write(self, settings, control, fake_store):
        applier = DropApplier(fake_store, drop_on={1})
        lp = self._loop(settings, control, fake_store, applier)
        tick(lp)                                  # target A: 1st heat 70, dropped
        control.update_zone("zone:2", day_d=71, night_d=71)   # rules now want target B for 1st
        tick(lp)
        st = control.state()
        assert st["expected"]["zones"]["zone:2"] == {"name": "1st", "htsp": 71.0, "clsp": 73.0}
        assert st["write_attempts"] == 1          # confirmation restarts against B, not A
        assert applier.calls[-1]["zones"]["zone:2"]["htsp"] == 71.0
        assert applier.previous[-1]["zones"]["zone:2"]["htsp"] == 66.0   # diffed against live, so 1st is sent
        assert fake_store.latest("X", "zone:2", "htsp") == 71.0
        tick(lp)                                  # B confirmed; A is never chased again
        assert control.state()["override"] is None and len(applier.calls) == 2
        assert events(control, "retry") == [
            f"write not applied (attempt 1 of {MAX_WRITE_ATTEMPTS}): 1st heat setpoint is 66.0, expected 70;"
            " rewriting what is missing"]

    def test_dropped_then_newer_target_also_dropped_counts_from_one(self, settings, control, fake_store):
        applier = DropApplier(fake_store, drop_on={1, 2})
        lp = self._loop(settings, control, fake_store, applier)
        tick(lp)                                  # A, dropped
        control.update_zone("zone:2", day_d=71, night_d=71)
        tick(lp)                                  # B, dropped too: attempt 1 of B
        assert control.state()["write_attempts"] == 1
        tick(lp)                                  # attempt 2 of B lands
        assert control.state()["write_attempts"] == 2 and fake_store.latest("X", "zone:2", "htsp") == 71.0
        tick(lp)
        assert control.settings()["enabled"] is True and control.state()["write_attempts"] == 1

    def test_enable_sends_only_what_differs_from_live(self, loop, control, applier, fake_store):
        control.set_enabled(True)
        tick(loop)                                # thermostat already at 70/72 heat, hold on
        assert applier.previous[-1] == {"mode": "heat", "zones": {
            "zone:1": {"name": "Boys", "htsp": 70.0, "clsp": 72.0},
            "zone:2": {"name": "1st", "htsp": 70.0, "clsp": 72.0}}}
        assert events(control, "write") == ["wrote 2 zones, mode heat"]  # RecApplier ignores previous

    def test_zone_without_hold_is_sent_in_full(self, loop, control, applier, fake_store):
        fake_store.set_zone("zone:2", hold="off")
        control.set_enabled(True)
        tick(loop)
        assert "zone:2" not in applier.previous[-1]["zones"] and "zone:1" in applier.previous[-1]["zones"]

    def test_failed_write_resets_attempts(self, settings, control, fake_store):
        applier = RecApplier(fail_on={2}, mirror=fake_store)
        lp = ControlLoop(settings, fake_store, control, applier, serial="X")  # type: ignore[arg-type]
        lp.grace = 0
        control.set_enabled(True)
        tick(lp)
        fake_store.set_zone("zone:2", rt=73.0)
        tick(lp)                                  # raises
        assert control.state()["write_attempts"] is None
        tick(lp)
        assert control.state()["write_attempts"] == 1


class TestEvalSnapshot:
    def test_last_eval_contents(self, loop, control, fake_store):
        control.set_enabled(True)
        tick(loop)
        ev = control.state()["last_eval"]
        assert ev["thermostat_mode"] == "heat" and ev["oat"] == 74.0 and ev["mode"] == "heat"
        z = ev["zones"][0]
        assert z["name"] == "Boys" and (z["lo"], z["d"], z["hi"]) == (69, 70, 71) and z["period"] in ("day", "night")
        assert ev["lean"] is None and ev["lean_for"] == 0.0 and ev["mismatch"] is None

    def test_check_line_every_tick(self, loop, control):
        control.set_enabled(True)
        tick(loop)
        line = events(control, "check")[-1]
        assert line.startswith("heat (outdoor 74): Boys 70, 1st 70 | want 70 (69–71), 70 (69–71)")

    def test_decision_logged_only_on_change(self, loop, control, fake_store):
        control.set_enabled(True)
        tick(loop); tick(loop)
        assert events(control, "decision") == []          # "keep mode" from the start is not news
        fake_store.set_zone("zone:1", rt=68.0)
        tick(loop); tick(loop)
        assert len(events(control, "decision")) == 1      # asked for heat, once
        assert events(control, "decision")[0].startswith("rules ask for heat")
        fake_store.set_zone("zone:1", rt=70.0)
        tick(loop)
        assert events(control, "decision")[-1].startswith("rules ask for no change")
