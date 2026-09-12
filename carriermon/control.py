"""Custom heat/cool controller.

The thermostat's own Auto mode picks a direction from the cooling demand of the
warmest zones and ignores zones sitting under their heat setpoint. This loop
replaces that one decision — heat or cool — and leaves everything else to the
thermostat: it holds every zone at the same target ``T``, so the thermostat's
per-zone demand steers the dampers.

Rules (evaluated every minute; O = outdoor temp, band = 2 °F):
  1. any zone >= T+2 and O > T-10  -> cool     (a hot zone, unless it's much colder out)
  2. any zone <= T-2 and O < T+10  -> heat     (a cold zone, unless it's much hotter out)
     if both 1 and 2 hold, the larger error wins; equal -> outdoor decides
  3. every zone <= T and some zone < T, and that has held for 5 min -> heat
     every zone >= T and some zone > T, and that has held for 5 min -> cool
  4. O >= T+2 -> cool ; O <= T-2 -> heat       (no demand: pre-position toward outdoors)
  5. otherwise keep the mode the thermostat is in
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
from typing import Any, Protocol

from .controldb import ControlStore
from .db import Store
from .settings import Settings

log = logging.getLogger(__name__)

BAND = 2.0        # zone error (°F) that counts as demand
OAT_BAND = 2.0    # outdoor hysteresis for the no-demand rule
FAR = 10.0        # don't fight the outdoors when it is this far the other way
DEFAULT_GAP = 2.0 # thermostat deadband if config doesn't say
PERSIST = 5 * 60  # seconds a whole-house lean (rule 3) must hold before acting on it


# ---------------------------------------------------------------- decision
@dataclass(frozen=True)
class Decision:
    mode: str | None   # "heat" | "cool" | None = keep what the thermostat is doing
    rule: str
    hot: float | None = None   # largest excess above target
    cold: float | None = None  # largest deficit below target


def lean(target: float, temps: list[float]) -> str | None:
    """Which side of the target the whole house is on: 'below' if every zone is at or
    under T and at least one is under, 'above' for the mirror, else None."""
    if temps and all(t <= target for t in temps) and any(t < target for t in temps):
        return "below"
    if temps and all(t >= target for t in temps) and any(t > target for t in temps):
        return "above"
    return None


def decide(target: float, zones: dict[str, float | None], oat: float | None,
           lean_side: str | None = None, lean_for: float = 0.0) -> Decision:
    """Pure rule evaluation. ``zones`` maps display name -> room temp; ``lean_side`` /
    ``lean_for`` say which way the whole house leans (see ``lean``) and for how many
    seconds that has been true — the loop tracks it, since it needs memory."""
    temps = {name: rt for name, rt in zones.items() if rt is not None}
    if not temps:
        return Decision(None, "no zone temperatures available")
    hot_zone, hot_rt = max(temps.items(), key=lambda kv: kv[1])
    cold_zone, cold_rt = min(temps.items(), key=lambda kv: kv[1])
    hot, cold = hot_rt - target, target - cold_rt
    oat_s = "n/a" if oat is None else f"{oat:g}"

    cool_ok = hot >= BAND and (oat is None or oat > target - FAR)
    heat_ok = cold >= BAND and (oat is None or oat < target + FAR)
    cool_why = f"{hot_zone} is {hot_rt:g} (≥ {target + BAND:g}), outdoor {oat_s}"
    heat_why = f"{cold_zone} is {cold_rt:g} (≤ {target - BAND:g}), outdoor {oat_s}"

    if cool_ok and heat_ok:
        if hot > cold:
            return Decision("cool", f"both sides out of band; larger error is hot: {cool_why}", hot, cold)
        if cold > hot:
            return Decision("heat", f"both sides out of band; larger error is cold: {heat_why}", hot, cold)
        if oat is not None and oat > target:
            return Decision("cool", f"both sides out of band by {hot:g}; outdoor {oat_s} > target", hot, cold)
        if oat is not None and oat < target:
            return Decision("heat", f"both sides out of band by {cold:g}; outdoor {oat_s} < target", hot, cold)
        return Decision(None, f"both sides out of band by {hot:g}; outdoor at target — keep mode", hot, cold)
    if cool_ok:
        return Decision("cool", f"hot zone: {cool_why}", hot, cold)
    if heat_ok:
        return Decision("heat", f"cold zone: {heat_why}", hot, cold)
    # No actionable demand. Say why a hot/cold zone was ignored, if one was.
    if hot >= BAND:
        no_demand = f"ignoring hot {hot_zone} ({hot_rt:g}): outdoor {oat_s} is ≤ {target - FAR:g}"
    elif cold >= BAND:
        no_demand = f"ignoring cold {cold_zone} ({cold_rt:g}): outdoor {oat_s} is ≥ {target + FAR:g}"
    else:
        no_demand = f"all zones within ±{BAND:g}"
    if lean_side and lean_for >= PERSIST:
        mins = int(lean_for / 60)
        if lean_side == "below":
            return Decision("heat", f"{no_demand}; every zone ≤ {target:g} and {cold_zone} below it for {mins} min", hot, cold)
        return Decision("cool", f"{no_demand}; every zone ≥ {target:g} and {hot_zone} above it for {mins} min", hot, cold)
    lean_s = f"; house leaning {lean_side} for {int(lean_for / 60)} min" if lean_side else ""
    if oat is not None and oat >= target + OAT_BAND:
        return Decision("cool", f"{no_demand}{lean_s}; outdoor {oat_s} ≥ {target + OAT_BAND:g}", hot, cold)
    if oat is not None and oat <= target - OAT_BAND:
        return Decision("heat", f"{no_demand}{lean_s}; outdoor {oat_s} ≤ {target - OAT_BAND:g}", hot, cold)
    return Decision(None, f"{no_demand}; outdoor {oat_s} near target{lean_s} — keep mode", hot, cold)


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
        target = float(cfg["target"])
        # Rule 3 needs to know how long the house has leaned one way; remember when it
        # started, and start over if the lean flips or the target moves.
        side = lean(target, [z["rt"] for z in live.zones if z["rt"] is not None])
        if side != state["lean_side"] or target != state["lean_target"] or not state["lean_since"]:
            self.control.set_state(lean_side=side, lean_since=now if side else None, lean_target=target)
            lean_since = now if side else None
        else:
            lean_since = state["lean_since"]
        lean_for = now - lean_since if lean_since else 0.0
        decision = decide(target, {z["name"]: z["rt"] for z in live.zones}, live.oat, side, lean_for)
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
            writes = await self.applier.apply(serial, desired, expected)
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
        self.control.log("check", f"{mode} (target {target:g}, outdoor {oat_s}): {temps} — {decision.rule}")
        self.control.set_state(last_eval_ts=now, last_eval={
            "ts": now, "target": target, "oat": live.oat, "thermostat_mode": live.mode, "gap": live.gap,
            "zones": [{k: z[k] for k in ("name", "rt", "htsp", "clsp", "hold")} for z in live.zones],
            "hot": decision.hot, "cold": decision.cold, "lean": side, "lean_for": lean_for,
            "decision": decision.mode, "decision_rule": decision.rule,
            "mode": mode, "rule": rule,
            "mismatch": "; ".join(mismatches(live, state["expected"])) if state["expected"] else None,
        })


async def run_standalone(settings: Settings) -> None:
    """``carriermon control``: the loop on its own, always dry-run."""
    store = Store(settings.db_path, read_only=True)
    control = ControlStore(settings.control_db_path)
    await ControlLoop(settings, store, control, DryRunApplier()).run()
