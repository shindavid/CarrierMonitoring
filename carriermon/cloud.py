"""Cloud ingest: carrier_api websocket push + periodic full poll -> Store."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from carrier_api import ApiConnectionGraphql, System, WebsocketDataUpdater

from .db import Store
from .normalize import diff_rows, snapshot
from .settings import Settings

log = logging.getLogger(__name__)


class CloudIngest:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.api: ApiConnectionGraphql | None = None  # created inside the event loop
        self.systems: list[System] = []
        self.last: dict[str, dict] = {}
        self.updater: WebsocketDataUpdater | None = None

    # -- recording -------------------------------------------------------
    def record_system(self, system: System, source: str, force_all: bool) -> None:
        serial = system.profile.serial
        last = self.last.setdefault(serial, self.store.last_values(serial))
        values = snapshot(system.status.raw, system.config.raw)
        rows, changed = diff_rows(serial, values, last, source, force_all)
        self.store.add_readings(rows)
        if changed:
            log.info("%s: %d changed value(s) via %s", serial, changed, source)

    async def full_load(self, source: str) -> None:
        assert self.api is not None
        self.systems = await self.api.load_data()
        for system in self.systems:
            self.store.add_raw(source, system.profile.serial, {
                "profile": system.profile.raw,
                "status": system.status.raw,
                "config": system.config.raw,
                "energy": system.energy.raw,
            })
            self.record_system(system, source, force_all=True)
        if self.updater is not None:
            self.updater.systems = self.systems

    # -- websocket -------------------------------------------------------
    async def on_ws_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except ValueError:
            payload = {"_unparsed": message}
        serial = payload.get("serial") if isinstance(payload, dict) else None
        self.store.add_raw("cloud:ws", serial, message)
        if self.updater is None:
            return
        try:
            await self.updater.message_handler(message)
        except Exception:  # noqa: BLE001 - unknown serial / shape; raw is already saved
            log.exception("websocket message not applied: %s", message[:200])
            return
        for system in self.systems:
            if serial is None or system.profile.serial == serial:
                self.record_system(system, "cloud:ws", force_all=False)

    # -- main loop -------------------------------------------------------
    async def run(self) -> None:
        # aiohttp's ClientSession must be created while the loop is running.
        self.api = ApiConnectionGraphql(self.settings.username, self.settings.password)
        ws = None
        try:
            await self.full_load("cloud:load")
            self.updater = WebsocketDataUpdater(systems=self.systems)
            ws = self.api.api_websocket
            if ws is None:
                raise RuntimeError("carrier_api did not create a websocket client after login")
            ws.callback_add(self.on_ws_message)
            await ws.create_task_listener()
            log.info("websocket listener started; polling every %ss", self.settings.poll_seconds)
            while True:
                await asyncio.sleep(self.settings.poll_seconds)
                started = time.time()
                try:
                    await self.full_load("cloud:poll")
                    log.debug("poll ok in %.1fs", time.time() - started)
                except Exception:  # noqa: BLE001
                    log.exception("poll failed; will retry next interval")
        finally:
            if ws is not None:
                ws.running = False
                for task in (ws.task_listener, ws.task_heartbeat):
                    if task:
                        task.cancel()
            await self.api.cleanup()

async def probe(settings: Settings) -> dict:
    """One-shot: log in, fetch systems, return a summary of what the account exposes."""
    api = ApiConnectionGraphql(settings.username, settings.password)
    try:
        systems = await api.load_data()
        out = []
        for s in systems:
            p = s.profile
            out.append({
                "name": p.name, "serial": p.serial, "brand": p.brand, "model": p.model,
                "firmware": p.firmware,
                "indoor": {"model": p.indoor_model, "type": p.indoor_unit_type, "source": p.indoor_unit_source},
                "outdoor": {"model": p.outdoor_model, "type": p.outdoor_unit_type},
                "status_keys": sorted(s.status.raw.keys()),
                "idu": s.status.raw.get("idu"), "odu": s.status.raw.get("odu"),
                "zones": [
                    {k: z.get(k) for k in ("id", "name", "enabled", "rt", "rh", "htsp", "clsp", "fan",
                                            "zoneconditioning", "damperposition", "currentActivity")}
                    for z in s.status.raw.get("zones", [])
                ],
                "mode": s.status.raw.get("mode"), "oat": s.status.raw.get("oat"),
            })
        return {"systems": out}
    finally:
        await api.cleanup()
