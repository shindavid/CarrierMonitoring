"""Custom heat/cool controller.

The thermostat's own Auto mode picks a direction from the cooling demand of the
warmest zones and ignores zones sitting under their heat setpoint. This loop
replaces that one decision — heat or cool — and leaves everything else to the
thermostat.

Every zone Z has a desired temp D and a tolerable range [A, B] (A <= D <= B, B >= A+2),
one set for day and one for night, with its own day/night start times. The mode
decides which setpoint is live: heat mode writes heat = D (cool = D+2), cool mode
writes cool = D (heat = D-2), per zone. Rules (evaluated every minute; C(Z) = zone
temp, O = outdoor):
  1. any zone outside its tolerable range -> heat or cool toward it. A zone above and
     a zone below at once: the larger error (distance outside the range) wins; equal
     -> O > every C(Z)+1 cools, O < every C(Z)-1 heats, else keep the current mode.
  2. every zone tolerable: if every zone is strictly above its D ("leaning above") for
     5 minutes -> cool; every zone strictly below its D for 5 minutes -> heat. The
     zone just brought to D reads exactly D, which blocks the opposite lean until the
     whole house has drifted past D the other way — that is the hysteresis.
  3. otherwise keep the current mode.

Manual overrides: the loop remembers exactly what it wrote (mode, per-zone
setpoints, hold). If the live state does not match after a grace period, the
readings history decides what happened. A value the thermostat showed at some
point since the write and then moved away from was changed by someone at the
thermostat or in the Carrier app; the controller then switches itself off and
stays off until re-enabled from the control page. A value the thermostat never
showed is a write Carrier accepted but the thermostat did not apply (this happens
a few percent of the time, usually to one setpoint of a burst); the loop rewrites
just the parts that are missing, up to MAX_WRITE_ATTEMPTS, and only then gives up
and switches off. If the rules pick new setpoints while a write is still
unconfirmed, the new target simply replaces the old one: confirmation is always
judged against the most recent write, and every write sends whatever the
thermostat does not currently show, so nothing from a dropped write is skipped.

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

MARGIN = 1.0      # outdoor must be more than this beyond every zone temp to count as "outside"
PERSIST = 5 * 60  # seconds a whole-house lean must hold before it changes the mode
DEFAULT_GAP = 2.0 # thermostat deadband if config doesn't say
MAX_WRITE_ATTEMPTS = 3  # rewrites of one target the thermostat keeps ignoring before giving up


# ---------------------------------------------------------------- decision
@dataclass(frozen=True)
class Decision:
    mode: str | None   # "heat" | "cool" | None = keep what the thermostat is doing
    rule: str
    hot: float | None = None   # how far the worst zone is above its range (<= 0: none is)
    cold: float | None = None  # how far the worst zone is below its range


@dataclass(frozen=True)
class ZoneEval:
    name: str
    rt: float | None
    lo: float   # tolerable range
    d: float    # desired
    hi: float


def lean(zones: list[ZoneEval]) -> str | None:
    """'above' if every zone (with a reading) is strictly above its desired temp,
    'below' if every zone is strictly below, else None."""
    known = [z for z in zones if z.rt is not None]
    if known and all(z.rt > z.d for z in known):
        return "above"
    if known and all(z.rt < z.d for z in known):
        return "below"
    return None


def decide(zones: list[ZoneEval], oat: float | None, lean_side: str | None = None, lean_for: float = 0.0) -> Decision:
    """Pure rule evaluation. ``lean_side``/``lean_for`` (see ``lean``) come from the loop,
    which tracks how long the house has leaned one way."""
    known = [z for z in zones if z.rt is not None]
    if not known:
        return Decision(None, "no zone temperatures available")
    above = max(known, key=lambda z: z.rt - z.hi)   # most above its range
    below = max(known, key=lambda z: z.lo - z.rt)   # most below its range
    hot, cold = above.rt - above.hi, below.lo - below.rt
    too_hot, too_cold = hot > 0, cold > 0
    max_c, min_c = max(z.rt for z in known), min(z.rt for z in known)
    oat_s = "n/a" if oat is None else f"{oat:g}"
    out_cool = oat is not None and oat > max_c + MARGIN
    out_heat = oat is not None and oat < min_c - MARGIN
    cool_why = f"{above.name} is {above.rt:g}, above its {above.lo:g}–{above.hi:g}"
    heat_why = f"{below.name} is {below.rt:g}, below its {below.lo:g}–{below.hi:g}"

    if too_hot and too_cold:
        if hot > cold:
            return Decision("cool", f"zones out on both sides; larger error is hot: {cool_why}", hot, cold)
        if cold > hot:
            return Decision("heat", f"zones out on both sides; larger error is cold: {heat_why}", hot, cold)
        if out_cool:
            return Decision("cool", f"zones {hot:g} out on both sides; outdoor {oat_s} above every zone", hot, cold)
        if out_heat:
            return Decision("heat", f"zones {cold:g} out on both sides; outdoor {oat_s} below every zone", hot, cold)
        return Decision(None, f"zones {hot:g} out on both sides; outdoor {oat_s} among the zones — keep mode", hot, cold)
    if too_hot:
        return Decision("cool", f"hot zone: {cool_why}", hot, cold)
    if too_cold:
        return Decision("heat", f"cold zone: {heat_why}", hot, cold)
    mins = int(lean_for / 60)
    if lean_side == "above" and lean_for >= PERSIST:
        return Decision("cool", f"all zones tolerable; every zone above its desired temp for {mins} min", hot, cold)
    if lean_side == "below" and lean_for >= PERSIST:
        return Decision("heat", f"all zones tolerable; every zone below its desired temp for {mins} min", hot, cold)
    lean_s = f"; every zone {lean_side} its desired temp for {mins} min" if lean_side else ""
    return Decision(None, f"all zones tolerable{lean_s} — keep mode", hot, cold)


def period_now(day_start: str, night_start: str, now: datetime | None = None) -> str:
    """'day' or 'night' by wall clock. Times are 'HH:MM'; day runs from day_start up to
    night_start, wrapping midnight if night_start is the earlier of the two."""
    t = (now or datetime.now()).strftime("%H:%M")
    if day_start <= night_start:
        return "day" if day_start <= t < night_start else "night"
    return "day" if (t >= day_start or t < night_start) else "night"


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


@dataclass(frozen=True)
class Mismatch:
    entity: str      # "system" for the mode, else the zone entity
    field: str       # mode | htsp | clsp | hold
    want: Any
    message: str


def mismatch_items(live: Live, expected: dict) -> list[Mismatch]:
    """Every field where the thermostat reports something other than what we wrote."""
    out = []
    if live.mode != expected["mode"]:
        out.append(Mismatch("system", "mode", expected["mode"], f"mode is {live.mode}, expected {expected['mode']}"))
    by_entity = {z["entity"]: z for z in live.zones}
    for entity, want in expected["zones"].items():
        z = by_entity.get(entity)
        if z is None:
            continue
        for field, label in (("htsp", "heat setpoint"), ("clsp", "cool setpoint")):
            have = z[field]
            if not isinstance(have, (int, float)) or abs(have - want[field]) > 0.01:
                out.append(Mismatch(entity, field, want[field],
                                    f"{z['name']} {label} is {have}, expected {want[field]:g}"))
        if z["hold"] != "on":
            out.append(Mismatch(entity, "hold", "on", f"{z['name']} hold is {z['hold']}, expected on"))
    return out


def mismatches(live: Live, expected: dict) -> list[str]:
    """Differences between what we wrote and what the thermostat reports now."""
    return [m.message for m in mismatch_items(live, expected)]


def live_as_previous(live: Live) -> dict:
    """The thermostat's current state in the shape of a write, for the applier to
    diff against: it then sends only what the thermostat does not already show. A
    zone without hold on is left out so it is written in full (setpoints and hold)."""
    return {"mode": live.mode, "zones": {
        z["entity"]: {"name": z["name"], "htsp": z["htsp"], "clsp": z["clsp"]}
        for z in live.zones if z["hold"] == "on"}}


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
                self.control.set_state(expected=None, written_ts=None, write_attempts=None, mode=None,
                                       mode_since=None, rule=None, lean_side=None, lean_since=None)
                self.control.log("disabled", "controller switched off; thermostat left as is")
            return

        serial = self.serial or (self.store.serials() or [None])[0]
        if serial is None:
            self.control.set_state(last_eval_ts=now, last_eval={"error": "no system in readings database"})
            return
        live = read_live(self.store, serial)

        # -- override detection: does the thermostat show what we wrote? --
        expected = state["expected"]
        attempts = state["write_attempts"] or 0
        retry = False  # rewrite the current target this tick because the thermostat never took it
        if expected is not None and state["written_ts"]:
            items = mismatch_items(live, expected)
            if not items:
                if attempts > 1:
                    self.control.log("write", f"thermostat now shows what was written (took {attempts} attempts)")
                    self.control.set_state(write_attempts=1)
            elif now - state["written_ts"] > self.grace:
                reason = "; ".join(m.message for m in items)
                if self.applier.dry_run:
                    # Nothing was really written, so live can never match: report, don't trip.
                    if state["last_eval"] and state["last_eval"].get("mismatch") != reason:
                        self.control.log("override", f"(dry run) would switch off: {reason}")
                elif self.ever_applied(serial, items, state["written_ts"]):
                    # The thermostat did show it and then moved: a person changed it.
                    self.control.trip_override(reason)
                    self.control.log("override", f"manual change detected, controller switched off: {reason}")
                    self.control.set_state(mode=None, mode_since=None, rule=None)
                    return
                elif attempts >= MAX_WRITE_ATTEMPTS:
                    why = f"thermostat did not apply the controller's settings after {attempts} attempts ({reason})"
                    self.control.trip_override(why)
                    self.control.log("override", f"write never applied, controller switched off: {reason}")
                    self.control.set_state(mode=None, mode_since=None, rule=None)
                    return
                else:
                    retry = True
                    self.control.log("retry", f"write not applied (attempt {attempts} of {MAX_WRITE_ATTEMPTS}): "
                                              f"{reason}; rewriting what is missing")

        # -- decide --
        # Each zone's desired temp and tolerable range for right now (its own day/night schedule).
        ranges = self.control.zones([z["entity"] for z in live.zones])
        evals: list[ZoneEval] = []
        periods: dict[str, str] = {}
        for z in live.zones:
            r = ranges[z["entity"]]
            period = period_now(r["day_start"], r["night_start"])
            periods[z["entity"]] = period
            evals.append(ZoneEval(z["name"], z["rt"], r[f"{period}_lo"], r[f"{period}_d"], r[f"{period}_hi"]))
        # Rule 2 needs to know how long the house has leaned; remember when it started.
        side = lean(evals)
        if side != state["lean_side"] or not state["lean_since"]:
            lean_since = now if side else None
            self.control.set_state(lean_side=side, lean_since=lean_since)
        else:
            lean_since = state["lean_since"]
        lean_for = now - lean_since if lean_since else 0.0
        decision = decide(evals, live.oat, side, lean_for)
        if decision.mode is not None:
            mode, rule = decision.mode, decision.rule
        elif state["mode"] in ("heat", "cool"):
            mode, rule = state["mode"], decision.rule
        elif live.mode in ("heat", "cool"):
            mode, rule = live.mode, decision.rule + f"; thermostat already in {live.mode}"
        else:
            temps = [z.rt for z in evals if z.rt is not None]
            mean = sum(temps) / len(temps) if temps else None
            mode = "cool" if (live.oat is not None and mean is not None and live.oat >= mean) else "heat"
            rule = decision.rule + f"; thermostat in {live.mode}, picking {mode} from outdoor temp"

        if decision.mode != state["last_decision_mode"]:
            self.control.log("decision", f"rules ask for {decision.mode or 'no change'}: {decision.rule}")
            self.control.set_state(last_decision_mode=decision.mode)

        # -- desired state and writes --
        # The live setpoint sits at each zone's desired temp; the other one only has to
        # respect the thermostat's deadband (it never drives equipment in this mode).
        def sp(ev: ZoneEval) -> dict:
            if mode == "cool":
                return {"htsp": ev.d - live.gap, "clsp": ev.d}
            return {"htsp": ev.d, "clsp": ev.d + live.gap}
        desired = {"mode": mode, "zones": {
            z["entity"]: {"name": z["name"], **sp(ev)} for z, ev in zip(live.zones, evals)}}
        new_target = expected is None or expected["mode"] != mode or expected["zones"] != desired["zones"]
        if not new_target and not retry:
            pass  # nothing to do
        else:
            # Diff against what the thermostat shows, not against what we last sent:
            # a dropped field from an earlier write is then written again, and a
            # retry sends only the parts still missing.
            try:
                writes = await self.applier.apply(serial, desired, live_as_previous(live))
            except Exception as exc:  # noqa: BLE001 - Carrier's API times out now and then
                # Some of the batch may have landed, so what the thermostat holds is now
                # unknown. Forget our claim on it (no override check against stale
                # expectations) and rewrite everything next tick.
                self.control.set_state(expected=None, written_ts=now, write_attempts=None, mode=mode, rule=rule)
                self.control.log("error", f"write failed, will retry next check: {type(exc).__name__}: {exc}")
                log.warning("control write failed: %s", exc)
                return
            for w in writes:
                self.control.log("write", w)
            mode_changed = expected is None or expected["mode"] != mode
            # A new target supersedes an unconfirmed one: confirmation restarts against it.
            fields: dict[str, Any] = {"expected": desired, "written_ts": now, "applied_settings_ts": cfg["updated_ts"],
                                      "write_attempts": 1 if new_target else attempts + 1, "mode": mode, "rule": rule}
            if mode_changed:
                fields["mode_since"] = now
                self.control.log("mode", f"{'(dry run) ' if self.applier.dry_run else ''}mode → {mode}: {rule}")
            self.control.set_state(**fields)

        temps = ", ".join(f"{ev.name} {ev.rt:g}" if ev.rt is not None else f"{ev.name} ?" for ev in evals)
        temps += " | want " + ", ".join(f"{ev.d:g} ({ev.lo:g}–{ev.hi:g})" for ev in evals)
        oat_s = "?" if live.oat is None else f"{live.oat:g}"
        self.control.log("check", f"{mode} (outdoor {oat_s}): {temps} — {decision.rule}")
        self.control.set_state(last_eval_ts=now, last_eval={
            "ts": now, "oat": live.oat, "thermostat_mode": live.mode, "gap": live.gap,
            "zones": [{**{k: z[k] for k in ("entity", "name", "rt", "htsp", "clsp", "hold")},
                       "lo": ev.lo, "d": ev.d, "hi": ev.hi, "period": periods[z["entity"]]} for z, ev in zip(live.zones, evals)],
            "hot": decision.hot, "cold": decision.cold, "lean": side, "lean_for": lean_for,
            "decision": decision.mode, "decision_rule": decision.rule,
            "mode": mode, "rule": rule,
            "mismatch": "; ".join(mismatches(live, state["expected"])) if state["expected"] else None,
        })

    def ever_applied(self, serial: str, items: list[Mismatch], since: float) -> bool:
        """Did the thermostat report any of these wanted values at some point since the
        write? Then it took the write and someone changed it afterwards. False when
        nothing we wrote was ever seen: the write did not land."""
        for m in items:
            seen = self.store.seen_since(serial, m.entity, m.field, since)
            if isinstance(m.want, (int, float)):
                if any(isinstance(v, (int, float)) and abs(v - m.want) <= 0.01 for v in seen):
                    return True
            elif m.want in seen:
                return True
        return False


async def run_standalone(settings: Settings) -> None:
    """``carriermon control``: the loop on its own, always dry-run."""
    store = Store(settings.db_path, read_only=True)
    control = ControlStore(settings.control_db_path)
    await ControlLoop(settings, store, control, DryRunApplier()).run()
