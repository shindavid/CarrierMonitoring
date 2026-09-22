"""Shared fixtures: a Settings pointing at temp files, a fake readings store, a
recording applier, and a ready-to-tick ControlLoop."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from carriermon.control import ControlLoop
from carriermon.controldb import ControlStore
from carriermon.db import Store
from carriermon.settings import Settings


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = dict(
        username=None, password=None, dev=True,
        db_path=tmp_path / "readings.sqlite", poll_seconds=300, retention_days=7,
        web_host="127.0.0.1", web_port=0, auth_user=None, auth_password=None,
        control_db_path=tmp_path / "control.sqlite", control_interval=60, control_dry_run=True,
        home_networks=(), public_ip_url=None, public_ip6_url=None,   # no network lookups in tests
        vapid_public_key=None, vapid_private_key=None, vapid_subject="mailto:test@localhost",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def control(settings: Settings) -> ControlStore:
    return ControlStore(settings.control_db_path)


class FakeStore:
    """Stands in for db.Store: the loop only needs serials(), zones() and latest()."""

    def __init__(self, zones: list[tuple[str, str]] | None = None) -> None:
        self.values: dict[tuple[str, str], Any] = {}
        self.history: list[tuple[float, str, str, Any]] = []   # every value ever set, like the readings table
        self.set("system", "mode", "heat"); self.set("system", "oat", 74.0); self.set("config", "cfgdead", 2.0)
        self._zones = zones or [("zone:1", "Boys"), ("zone:2", "1st")]
        for entity, _ in self._zones:
            self.set_zone(entity, rt=70.0, htsp=70.0, clsp=72.0, hold="on")

    def serials(self) -> list[str]:
        return ["X"]

    def zones(self, serial: str) -> list[dict]:
        return [{"entity": e, "name": n} for e, n in self._zones]

    def latest(self, serial: str, entity: str, field: str) -> Any:
        return self.values.get((entity, field))

    def seen_since(self, serial: str, entity: str, field: str, since: float) -> set[Any]:
        return {v for ts, e, f, v in self.history if e == entity and f == field and ts >= since}

    def set_zone(self, entity: str, **fields: Any) -> None:
        for k, v in fields.items():
            self.set(entity, k, v)

    def set(self, entity: str, field: str, value: Any) -> None:
        self.values[(entity, field)] = value
        self.history.append((time.time(), entity, field, value))

    def mirror(self, desired: dict) -> None:
        """Pretend the thermostat applied a controller write."""
        self.set("system", "mode", desired["mode"])
        for entity, sp in desired["zones"].items():
            self.set_zone(entity, htsp=sp["htsp"], clsp=sp["clsp"], hold="on")


class RecApplier:
    """Records writes; optionally fails on chosen call numbers (1-based)."""

    def __init__(self, dry_run: bool = False, fail_on: set[int] | None = None, mirror: FakeStore | None = None) -> None:
        self.dry_run = dry_run
        self.calls: list[dict] = []
        self.previous: list[dict | None] = []   # what the loop said the thermostat already showed
        self.fail_on = fail_on or set()
        self.mirror = mirror

    async def apply(self, serial: str, desired: dict, previous: dict | None) -> list[str]:
        self.calls.append(desired)
        self.previous.append(previous)
        if len(self.calls) in self.fail_on:
            raise RuntimeError("504 Gateway Timeout")
        if self.mirror is not None:
            self.mirror.mirror(desired)
        return [f"wrote {len(desired['zones'])} zones, mode {desired['mode']}"]


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def applier(fake_store: FakeStore) -> RecApplier:
    return RecApplier(dry_run=False, mirror=fake_store)


@pytest.fixture
def loop(settings: Settings, fake_store: FakeStore, control: ControlStore, applier: RecApplier) -> ControlLoop:
    lp = ControlLoop(settings, fake_store, control, applier, serial="X")  # type: ignore[arg-type]
    lp.grace = 0  # believe the live state immediately in tests
    return lp


def tick(loop: ControlLoop) -> None:
    asyncio.run(loop.tick())


def populate_readings(path: Path, serial: str = "S1") -> Store:
    """A readings database with two enabled zones, a disabled one, and some history."""
    store = Store(path)
    t0 = 1_000_000.0
    rows = [
        (t0, serial, "config.zone:1", "name", None, "Upstairs", 1, "cloud:load"),
        (t0, serial, "config.zone:2", "name", None, "Downstairs", 1, "cloud:load"),
        (t0, serial, "config.zone:3", "name", None, "Garage", 1, "cloud:load"),
        (t0, serial, "zone:1", "enabled", None, "on", 1, "cloud:load"),
        (t0, serial, "zone:2", "enabled", None, "on", 1, "cloud:load"),
        (t0, serial, "zone:3", "enabled", None, "off", 1, "cloud:load"),
        (t0, serial, "zone:1", "rt", 70.0, None, 1, "cloud:load"),
        (t0 + 60, serial, "zone:1", "rt", 71.0, None, 1, "cloud:ws"),
        (t0 + 120, serial, "zone:1", "rt", 71.0, None, 0, "cloud:poll"),
        (t0 + 180, serial, "zone:1", "rt", 72.0, None, 1, "cloud:ws"),
        (t0, serial, "zone:1", "htsp", 69.0, None, 1, "cloud:load"),
        (t0, serial, "zone:1", "clsp", 71.0, None, 1, "cloud:load"),
        (t0, serial, "zone:1", "hold", None, "off", 1, "cloud:load"),
        (t0, serial, "zone:2", "rt", 68.0, None, 1, "cloud:load"),
        (t0, serial, "system", "mode", None, "auto", 1, "cloud:load"),
        (t0 + 200, serial, "system", "mode", None, "cool", 1, "cloud:ws"),
        (t0, serial, "system", "oat", 80.0, None, 1, "cloud:load"),
        (t0, serial, "config", "cfgdead", 2.0, None, 1, "cloud:load"),
    ]
    store.add_readings(rows)
    return store
