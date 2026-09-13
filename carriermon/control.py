"""Custom heat/cool controller.

The thermostat's own Auto mode picks a direction from the cooling demand of the
warmest zones and ignores zones sitting under their heat setpoint. This loop
replaces that one decision — heat or cool — and leaves everything else to the
thermostat: it holds every zone at the same target ``T``, so the thermostat's
per-zone demand steers the dampers.

Rules (evaluated every minute; the target T means the band [T-1, T+1]; O = outdoor):
  1. any zone outside the band -> heat or cool toward it. A zone above and a zone
     below at once: the larger error wins; equal -> O above T cools, below T heats.
  2. every zone in the band -> follow the outdoors: O above the band -> cool,
     O below the band -> heat.
  3. every zone in the band and O in the band -> keep the current mode.
The target has a daytime and a nighttime value; which applies depends on the
day-start / night-start times in the settings.
Setpoints written: cool mode -> cool T / heat T-gap ; heat mode -> heat T / cool T+gap,
where gap is the thermostat's configured deadband (2 °F).

Manual overrides: the loop remembers exactly what it wrote (mode, per-zone
setpoints, hold). If the live state stops matching after a grace period, someone
changed something at the thermostat or in the Carrier app; the controller then
switches itself off and stays off until re-enabled from the control page.

Hosting: in the production checkout the loop runs inside ``carriermon ingest``
(it already holds the Carrier session). ``carriermon control`` runs the same loop
standalone and is always dry-run — that is how a dev checkout exercises it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .controldb import ControlStore
from .db import Store
from .settings import Settings

log = logging.getLogger(__name__)

BAND = 1.0        # half-width of the comfort band around the target (°F)
DEFAULT_GAP = 2.0 # thermostat deadband if config doesn't say


# ---------------------------------------------------------------- decision
@dataclass(frozen=True)
class Decision:
    mode: str | None   # "heat" | "cool" | None = keep what the thermostat is doing
    rule: str
    hot: float | None = None   # largest excess above target
    cold: float | None = None  # largest deficit below target


def decide(target: float, zones: dict[str, float | None], oat: float | None) -> Decision:
    """Pure rule evaluation. ``zones`` maps display name -> room temp."""
    temps = {name: rt for name, rt in zones.items() if rt is not None}
    if not temps:
        return Decision(None, "no zone temperatures available")
    lo, hi = target - BAND, target + BAND
    band = f"{lo:g}–{hi:g}"
    hot_zone, hot_rt = max(temps.items(), key=lambda kv: kv[1])
    cold_zone, cold_rt = min(temps.items(), key=lambda kv: kv[1])
    hot, cold = hot_rt - target, target - cold_rt
    oat_s = "n/a" if oat is None else f"{oat:g}"
    too_hot, too_cold = hot_rt > hi, cold_rt < lo
    cool_why = f"{hot_zone} is {hot_rt:g}, above {band}"
    heat_why = f"{cold_zone} is {cold_rt:g}, below {band}"

    if too_hot and too_cold:
        if hot > cold:
            return Decision("cool", f"zones on both sides of {band}; larger error is hot: {cool_why}", hot, cold)
        if cold > hot:
            return Decision("heat", f"zones on both sides of {band}; larger error is cold: {heat_why}", hot, cold)
        if oat is not None and oat > target:
            return Decision("cool", f"zones {hot:g} out on both sides of {band}; outdoor {oat_s} > target", hot, cold)
        if oat is not None and oat < target:
            return Decision("heat", f"zones {cold:g} out on both sides of {band}; outdoor {oat_s} < target", hot, cold)
        return Decision(None, f"zones {hot:g} out on both sides of {band}; outdoor at target — keep mode", hot, cold)
    if too_hot:
        return Decision("cool", f"hot zone: {cool_why}", hot, cold)
    if too_cold:
        return Decision("heat", f"cold zone: {heat_why}", hot, cold)
    if oat is not None and oat > hi:
        return Decision("cool", f"all zones within {band}; outdoor {oat_s} above it", hot, cold)
    if oat is not None and oat < lo:
        return Decision("heat", f"all zones within {band}; outdoor {oat_s} below it", hot, cold)
    return Decision(None, f"all zones within {band}; outdoor {oat_s} in it — keep mode", hot, cold)


def period_now(day_start: str, night_start: str, now: datetime | None = None) -> str:
    """'day' or 'night' by wall clock. Times are 'HH:MM'; day runs from day_start up to
    night_start, wrapping midnight if night_start is the earlier of the two."""
    t = (now or datetime.now()).strftime("%H:%M")
    if day_start <= night_start:
        return "day" if day_start <= t < night_start else "night"
    return "day" if (t >= day_start or t < night_start) else "night"


def setpoints(mode: str, target: float, gap: float) -> dict[str, float]:
    """Both setpoints must be written (the thermostat enforces the deadband gap);
    only the one matching the mode ever drives equipment."""
    if mode == "cool":
        return {"htsp": target - gap, "clsp": target}
    return {"htsp": target, "clsp": target + gap}


# ---------------------------------------------------------------- live state
@dataclass
class Live:
    mode: str | None
    oat: float | None
    gap: float
    zones: list[dict]  # {entity, id, name, rt, htsp, clsp, hold}


def read_live(store: Store, serial: str) -> Live:
    """Current thermostat state from the readings database (updated by ingest)."""
    def latest(entity: str, field: str) -> Any:
        return store.latest(serial, entity, field)

    zones = []
    for z in store.zones(serial):
        entity = z["entity"]
        zones.append({
            "entity": entity, "id": entity.split(":")[1], "name": z["name"],
            "rt": latest(entity, "rt"), "htsp": latest(entity, "htsp"), "clsp": latest(entity, "clsp"),
            "hold": latest(entity, "hold"),
        })
    gap = latest("config", "cfgdead")
    return Live(
        mode=latest("system", "mode"), oat=latest("system", "oat"),
        gap=float(gap) if isinstance(gap, (int, float)) else DEFAULT_GAP, zones=zones,
    )


def mismatches(live: Live, expected: dict) -> list[str]:
    """Differences between what we wrote and what the thermostat reports now."""
    out = []
    if live.mode != expected["mode"]:
        out.append(f"mode is {live.mode}, expected {expected['mode']}")
    by_entity = {z["entity"]: z for z in live.zones}
    for entity, want in expected["zones"].items():
        z = by_entity.get(entity)
        if z is None:
            continue
        for field, label in (("htsp", "heat setpoint"), ("clsp", "cool setpoint")):
            have = z[field]
            if not isinstance(have, (int, float)) or abs(have - want[field]) > 0.01:
                out.append(f"{z['name']} {label} is {have}, expected {want[field]:g}")
        if z["hold"] != "on":
            out.append(f"{z['name']} hold is {z['hold']}, expected on")
    return out


# ---------------------------------------------------------------- appliers
class Applier(Protocol):
    dry_run: bool

    async def apply(self, serial: str, desired: dict, previous: dict | None) -> list[str]:
        """Push ``desired`` to the thermostat. Returns a description of each write."""


class DryRunApplier:
    dry_run = True

    async def apply(self, serial: str, desired: dict, previous: dict | None) -> list[str]:
        return [f"would write: {w}" for w in describe_writes(desired, previous)]


class CarrierApplier:
    """Writes through carrier_api. Zones first (setpoints, then hold), mode last."""
    dry_run = False

    def __init__(self, api: Any) -> None:
        self.api = api

    async def apply(self, serial: str, desired: dict, previous: dict | None) -> list[str]:
        from carrier_api.const import ActivityTypes, SystemModes

        done = []
        for entity, sp in desired["zones"].items():
            if previous and previous["zones"].get(entity) == sp:
                continue
            zone_id = entity.split(":")[1]
            await self.api.set_config_activity(serial, zone_id, ActivityTypes.MANUAL,
                                               _fmt(sp["htsp"]), _fmt(sp["clsp"]))
            await self.api.set_config_hold(serial, zone_id, ActivityTypes.MANUAL, None)
            done.append(f"{sp['name']}: heat {sp['htsp']:g} / cool {sp['clsp']:g}, hold on")
        if not previous or previous["mode"] != desired["mode"]:
            await self.api.set_config_mode(serial, SystemModes(desired["mode"]))
            done.append(f"mode {desired['mode']}")
        return done


def _fmt(value: float) -> str:
    return f"{int(value)}" if float(value).is_integer() else f"{value:g}"


def describe_writes(desired: dict, previous: dict | None) -> list[str]:
    out = []
    for entity, sp in desired["zones"].items():
        if previous and previous["zones"].get(entity) == sp:
            continue
        out.append(f"{sp['name']}: heat {sp['htsp']:g} / cool {sp['clsp']:g}, hold on")
    if not previous or previous["mode"] != desired["mode"]:
        out.append(f"mode {desired['mode']}")
    return out


# ---------------------------------------------------------------- loop
class ControlLoop:
    def __init__(self, settings: Settings, store: Store, control: ControlStore, applier: Applier,
                 serial: str | None = None) -> None:
        self.settings = settings
        self.store = store
        self.control = control
        self.applier = applier
        self.serial = serial
        # Live values arrive by websocket push, but a value that did not change is only
        # re-anchored by the next poll; wait for one before believing a mismatch.
        self.grace = settings.poll_seconds + 60

    async def run(self) -> None:
        log.info("control loop: every %ss, %s", self.settings.control_interval,
                 "DRY RUN (no writes)" if self.applier.dry_run else "LIVE")
        self.control.log("start", "controller loop started" + (" (dry run)" if self.applier.dry_run else ""))
        last_prune = 0.0
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - keep looping; the failure is logged
                log.exception("control tick failed")
                self.control.log("error", f"{type(exc).__name__}: {exc}")
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                self.control.prune_log()
            await asyncio.sleep(self.settings.control_interval)

    async def tick(self) -> None:
        now = time.time()
        self.control.heartbeat(self.applier.dry_run)
        cfg = self.control.settings()
        state = self.control.state()

        if not cfg["enabled"]:
            if state["expected"] is not None:
                # Switched off from the UI: leave the thermostat as it is, forget our claim on it.
                self.control.set_state(expected=None, written_ts=None, mode=None, mode_since=None, rule=None)
                self.control.log("disabled", "controller switched off; thermostat left as is")
            return

        serial = self.serial or (self.store.serials() or [None])[0]
        if serial is None:
            self.control.set_state(last_eval_ts=now, last_eval={"error": "no system in readings database"})
            return
        live = read_live(self.store, serial)

        # -- override detection: does the thermostat still show what we wrote? --
        if state["expected"] is not None and state["written_ts"] and now - state["written_ts"] > self.grace:
            diffs = mismatches(live, state["expected"])
            if diffs:
                reason = "; ".join(diffs)
                if self.applier.dry_run:
                    # Nothing was really written, so live can never match: report, don't trip.
                    if state["last_eval"] and state["last_eval"].get("mismatch") != reason:
                        self.control.log("override", f"(dry run) would switch off: {reason}")
                else:
                    self.control.trip_override(reason)
                    self.control.log("override", f"manual change detected, controller switched off: {reason}")
                    self.control.set_state(mode=None, mode_since=None, rule=None)
                    return

        # -- decide --
        period = period_now(cfg["day_start"], cfg["night_start"])
        target = float(cfg["target_day"] if period == "day" else cfg["target_night"])
        decision = decide(target, {z["name"]: z["rt"] for z in live.zones}, live.oat)
        if decision.mode is not None:
            mode, rule = decision.mode, decision.rule
        elif state["mode"] in ("heat", "cool"):
            mode, rule = state["mode"], decision.rule
        elif live.mode in ("heat", "cool"):
            mode, rule = live.mode, decision.rule + f"; thermostat already in {live.mode}"
        else:
            mode = "cool" if (live.oat is not None and live.oat >= target) else "heat"
            rule = decision.rule + f"; thermostat in {live.mode}, picking {mode} from outdoor temp"

        if decision.mode != state["last_decision_mode"]:
            self.control.log("decision", f"rules ask for {decision.mode or 'no change'}: {decision.rule}")
            self.control.set_state(last_decision_mode=decision.mode)

        # -- desired state and writes --
        desired = {"mode": mode, "target": target, "zones": {
            z["entity"]: {"name": z["name"], **setpoints(mode, target, live.gap)} for z in live.zones}}
        expected = state["expected"]
        if expected is not None and expected.get("target") == target and expected["mode"] == mode \
                and expected["zones"] == desired["zones"]:
            pass  # nothing to do
        else:
            try:
                writes = await self.applier.apply(serial, desired, expected)
            except Exception as exc:  # noqa: BLE001 - Carrier's API times out now and then
                # Some of the batch may have landed, so what the thermostat holds is now
                # unknown. Forget our claim on it (no override check against stale
                # expectations) and rewrite everything next tick.
                self.control.set_state(expected=None, written_ts=now, mode=mode, rule=rule)
                self.control.log("error", f"write failed, will retry next check: {type(exc).__name__}: {exc}")
                log.warning("control write failed: %s", exc)
                return
            for w in writes:
                self.control.log("write", w)
            mode_changed = expected is None or expected["mode"] != mode
            fields: dict[str, Any] = {"expected": desired, "written_ts": now, "applied_settings_ts": cfg["updated_ts"],
                                      "mode": mode, "rule": rule}
            if mode_changed:
                fields["mode_since"] = now
                self.control.log("mode", f"{'(dry run) ' if self.applier.dry_run else ''}mode → {mode}: {rule}")
            self.control.set_state(**fields)

        temps = ", ".join(f"{z['name']} {z['rt']:g}" if z["rt"] is not None else f"{z['name']} ?" for z in live.zones)
        oat_s = "?" if live.oat is None else f"{live.oat:g}"
        self.control.log("check", f"{mode} (target {target:g} {period}, outdoor {oat_s}): {temps} — {decision.rule}")
        self.control.set_state(last_eval_ts=now, last_eval={
            "ts": now, "target": target, "oat": live.oat, "thermostat_mode": live.mode, "gap": live.gap,
            "zones": [{k: z[k] for k in ("name", "rt", "htsp", "clsp", "hold")} for z in live.zones],
            "hot": decision.hot, "cold": decision.cold, "period": period,
            "decision": decision.mode, "decision_rule": decision.rule,
            "mode": mode, "rule": rule,
            "mismatch": "; ".join(mismatches(live, state["expected"])) if state["expected"] else None,
        })


async def run_standalone(settings: Settings) -> None:
    """``carriermon control``: the loop on its own, always dry-run."""
    store = Store(settings.db_path, read_only=True)
    control = ControlStore(settings.control_db_path)
    await ControlLoop(settings, store, control, DryRunApplier()).run()
