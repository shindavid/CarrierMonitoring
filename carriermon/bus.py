"""Bus ingest: zone sensor readings from an infinitesp ABCD-bus tap -> Store.

infinitesp (an ESP32 on the Carrier ABCD bus, flashed passive) streams every bus
frame as one JSON object per line on TCP port 2373, e.g.
``{"ts":"...","src":"60","dst":"20","func":"06","reg":"0302","data":"0101..."}``.
For now we keep only the zone temperature readings the thermostat polls:

- Zone controller (0x60 = zones 1-4, 0x61 = zones 5-8) replying to 0302: six
  ``[tag, id, hi, lo]`` entries; tag 0x01 = sensor present, ids 1-4 = the board's
  ZS1..ZS4 terminals (wired Remote Room Sensors); value / 16 = °F.
- Smart Sensors (0x21-0x2F) replying to 041E: bytes 9-10 = °F x 16.

These are the sensor values *before* the thermostat applies its zone offsets, so
they can differ from the cloud's zone ``rt`` by that offset. Smart Sensors are
stored by bus address (``bus.sensor:22``); a Smart Sensor's address is 0x20 plus the
zone address set on the sensor itself, so 0x22 serves zone 2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .db import Store
from .normalize import diff_rows
from .settings import Settings

log = logging.getLogger(__name__)

SOURCE = "bus:tap"
ZONE_CONTROLLERS = {0x60: 0, 0x61: 4}   # bus address -> zone number offset
SMART_SENSORS = range(0x21, 0x30)
PLAUSIBLE_F = (20.0, 120.0)            # outside this a decode is wrong, not a room
STALL_SECONDS = 30                      # the bus is never quiet this long; reconnect


def decode(frame: dict[str, Any]) -> dict[tuple[str, str], float]:
    """Zone temperature values carried by one bus frame (usually none)."""
    if frame.get("func") != "06" or frame.get("dst") != "20":   # only replies to the thermostat
        return {}
    try:
        addr = int(frame.get("src") or "", 16)
        raw = bytes.fromhex(frame.get("data") or "")
    except ValueError:
        return {}
    out: dict[tuple[str, str], float] = {}
    reg = frame.get("reg")
    if reg == "0302" and addr in ZONE_CONTROLLERS and len(raw) == 24:
        for i in range(0, 24, 4):
            tag, ident = raw[i], raw[i + 1]
            if tag == 0x01 and 1 <= ident <= 4:
                out[(f"bus.zone:{ident + ZONE_CONTROLLERS[addr]}", "rt")] = int.from_bytes(raw[i + 2:i + 4], "big") / 16
    elif reg == "041E" and addr in SMART_SENSORS and len(raw) >= 11:
        out[(f"bus.sensor:{addr:02x}", "rt")] = int.from_bytes(raw[9:11], "big") / 16
    return {k: v for k, v in out.items() if PLAUSIBLE_F[0] <= v <= PLAUSIBLE_F[1]}


class BusIngest:
    def __init__(self, settings: Settings, store: Store, serial: str) -> None:
        self.settings = settings
        self.store = store
        self.serial = serial
        self.last = store.last_values(serial)
        self.current: dict[tuple[str, str], float] = {}
        self.last_anchor = time.time()

    def handle_line(self, line: bytes, now: float | None = None) -> int:
        """Record one stream line; returns the number of rows written."""
        now = now or time.time()
        try:
            frame = json.loads(line)
        except ValueError:
            return 0
        values = decode(frame) if isinstance(frame, dict) else {}
        self.current.update(values)
        # Like the cloud poll: re-record every value once per interval so series
        # have sample points even when a temperature holds steady.
        if now - self.last_anchor >= self.settings.poll_seconds:
            self.last_anchor = now
            values, force = self.current, True
        else:
            force = False
        rows, _ = diff_rows(self.serial, values, self.last, SOURCE, force_all=force, ts=now)
        return self.store.add_readings(rows) if rows else 0

    def on_connect(self) -> None:
        """Forget the values from before a (re)connect: the gap may be an outage (the
        HVAC powered off), and an anchor must not re-record them as current."""
        self.current.clear()

    async def run(self) -> None:
        host, port = self.settings.bus_host, self.settings.bus_port
        backoff = 1
        while True:
            connected = False
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
                connected = True
                log.info("bus: connected to %s:%s", host, port)
                self.on_connect()
                backoff = 1
                try:
                    while True:
                        line = await asyncio.wait_for(reader.readline(), STALL_SECONDS)
                        if not line:
                            raise ConnectionError("stream closed")
                        self.handle_line(line)
                finally:
                    writer.close()
            except TimeoutError:   # before OSError: TimeoutError subclasses it
                reason = f"no bus data for {STALL_SECONDS}s" if connected else "connect timed out"
                log.warning("bus: %s; reconnecting in %ss", reason, backoff)
            except OSError as exc:
                log.warning("bus: %s; reconnecting in %ss", str(exc) or type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def zone_of(entity: str) -> int | None:
    """Zone number a bus entity reports for (``bus.zone:3`` -> 3, ``bus.sensor:23`` -> 3)."""
    kind, _, ident = entity.partition(":")
    try:
        if kind == "bus.zone":
            return int(ident)
        if kind == "bus.sensor":
            return int(ident, 16) - 0x20
    except ValueError:
        pass
    return None


def report(store: Store, minutes: float | None = None, now: float | None = None,
           zone_names: dict[int, str] | None = None) -> str:
    """The latest bus reading per sensor beside the cloud's zone temperature; with
    ``minutes``, also every change in that window. ``zone_names`` (from
    CARRIERMON_ZONE_NAMES) win over the names the cloud reports."""
    now = now or time.time()
    lines = []
    for serial in store.serials():
        names = {z["entity"]: z["name"] for z in store.zones(serial)}
        names.update({f"zone:{zone}": name for zone, name in (zone_names or {}).items()})
        entities = [r[0] for r in store.conn.execute(
            "SELECT DISTINCT entity FROM readings WHERE serial=? AND source=? AND field='rt'", (serial, SOURCE))]
        entities.sort(key=lambda e: (zone_of(e) or 99, e))
        lines.append(f"{'zone':<5}{'name':<14}{'sensor':<15}{'bus':>8}{'age':>7}{'cloud':>7}")
        for entity in entities:
            zone = zone_of(entity)
            ts, value = store.conn.execute(
                "SELECT ts, value_num FROM readings WHERE serial=? AND entity=? AND field='rt' "
                "ORDER BY ts DESC LIMIT 1", (serial, entity)).fetchone()
            cloud = store.latest(serial, f"zone:{zone}", "rt") if zone else None
            lines.append(f"{zone or '?':<5}{names.get(f'zone:{zone}', ''):<14}{entity:<15}{value:>8.2f}"
                         f"{_age(max(0.0, now - ts)):>7}{'' if cloud is None else f'{cloud:g}':>7}")
        if minutes:
            for entity in entities:
                changes = store.conn.execute(
                    "SELECT ts, value_num FROM readings WHERE serial=? AND entity=? AND field='rt' "
                    "AND changed=1 AND ts>=? ORDER BY ts", (serial, entity, now - minutes * 60)).fetchall()
                lines.append(f"\n{entity} ({names.get(f'zone:{zone_of(entity)}', '?')}), "
                             f"{len(changes)} change(s) in the last {minutes:g} min:")
                lines.extend(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(t))}  {v:.2f}" for t, v in changes)
    return "\n".join(lines) if lines else "no bus readings stored (is CARRIERMON_BUS_HOST set for ingest?)"


def _age(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"
