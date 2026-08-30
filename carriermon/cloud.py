"""Cloud ingest: carrier_api websocket push + periodic full poll -> Store."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from carrier_api import ApiConnectionGraphql, System, WebsocketDataUpdater
from gql import gql
from graphql import print_ast

from .db import Store
from .normalize import diff_rows, snapshot
from .settings import Settings

log = logging.getLogger(__name__)

# Zone status fields carrier_api's polled query omits but Carrier's schema provides.
# Without these, damper position only ever arrives via websocket pushes for zones
# that changed, so an idle zone never reports one.
EXTRA_ZONE_STATUS_FIELDS = ("damperposition", "occupancy", "otmr")


class ApiConnection(ApiConnectionGraphql):
    """carrier_api connection that asks for a few extra zone status fields."""

    async def authed_query(self, operation_name, query, variable_values):
        if operation_name == "getInfinitySystems":
            document = getattr(query, "document", query)  # gql v4 GraphQLRequest vs v3 DocumentNode
            text = print_ast(document)
            marker = "zoneconditioning"
            if marker in text and EXTRA_ZONE_STATUS_FIELDS[0] not in text:
                text = text.replace(marker, marker + "\n" + "\n".join(EXTRA_ZONE_STATUS_FIELDS), 1)
                query = gql(text)
        return await super().authed_query(operation_name, query, variable_values)


class CloudIngest:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.api: ApiConnectionGraphql | None = None  # created inside the event loop
        self.systems: list[System] = []
        self.last: dict[str, dict] = {}
        self.last_config_raw: dict[str, Any] = {}  # per-serial, to log config verbatim only on change
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
            serial = system.profile.serial
            raw: dict[str, Any] = {
                "profile": system.profile.raw,
                "status": system.status.raw,
                "energy": system.energy.raw,
            }
            # The ~34 KB config blob is near-static; logging it verbatim every poll was
            # ~89% of raw_messages. Keep it in the raw log only when it actually changes
            # (the last config-bearing raw before any timestamp reconstructs config then).
            if system.config.raw != self.last_config_raw.get(serial):
                raw["config"] = system.config.raw
                self.last_config_raw[serial] = system.config.raw
            self.store.add_raw(source, serial, raw)
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
        self.api = ApiConnection(self.settings.username, self.settings.password)
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
            last_prune = 0.0
            while True:
                await asyncio.sleep(self.settings.poll_seconds)
                started = time.time()
                try:
                    await self.full_load("cloud:poll")
                    log.debug("poll ok in %.1fs", time.time() - started)
                except Exception:  # noqa: BLE001
                    log.exception("poll failed; will retry next interval")
                # Enforce the retention window at most once a day (0 = keep forever).
                if self.settings.retention_days and time.time() - last_prune > 86400:
                    last_prune = time.time()
                    try:
                        deleted = self.store.prune(self.settings.retention_days)
                        if deleted:
                            log.info("pruned %d rows older than %d days",
                                     deleted, self.settings.retention_days)
                    except Exception:  # noqa: BLE001
                        log.exception("retention prune failed; will retry tomorrow")
        finally:
            if ws is not None:
                ws.running = False
                for task in (ws.task_listener, ws.task_heartbeat):
                    if task:
                        task.cancel()
            await self.api.cleanup()

async def probe(settings: Settings) -> dict:
    """One-shot: log in, fetch systems, return a summary of what the account exposes."""
    api = ApiConnection(settings.username, settings.password)
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
