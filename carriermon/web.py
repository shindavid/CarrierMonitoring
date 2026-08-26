"""FastAPI dashboard + JSON API over the readings store."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .db import Store
from .settings import Settings

STATIC = Path(__file__).parent / "static"

# Fields shown on the dashboard, per entity kind.
ZONE_NUMERIC = ["rt", "rh", "htsp", "clsp", "damperposition"]
ZONE_STATE = ["fan", "zoneconditioning", "hold", "currentActivity", "occupancy"]
SYSTEM_NUMERIC = ["oat", "filtrlvl", "humlvl"]
SYSTEM_STATE = ["mode", "humid"]
UNIT_FIELDS = ["opstat", "opmode", "cfm", "blwrpm", "statpress", "type"]


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path)
    app = FastAPI(title="carriermon")

    def _range(start: float | None, end: float | None) -> tuple[float, float]:
        end = end or time.time()
        start = start or end - 86400
        return start, end

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/systems")
    def systems() -> list[dict]:
        return [{"serial": s, "zones": store.zones(s)} for s in store.serials()]

    @app.get("/api/fields")
    def fields() -> list[dict]:
        return store.fields()

    @app.get("/api/series")
    def series(serial: str, entity: str, field: str, start: float | None = None, end: float | None = None) -> list[dict]:
        s, e = _range(start, end)
        return store.series(serial, entity, field, s, e)

    @app.get("/api/events")
    def events(serial: str | None = None, start: float | None = None, end: float | None = None,
               limit: int = Query(2000, le=20000)) -> list[dict]:
        s, e = _range(start, end)
        return store.events(serial, s, e, limit)

    @app.get("/api/dashboard")
    def dashboard(serial: str, start: float | None = None, end: float | None = None) -> dict:
        """Everything the main page needs in one call."""
        if serial not in store.serials():
            raise HTTPException(404, "unknown serial")
        s, e = _range(start, end)
        zones = store.zones(serial)
        out: dict = {"serial": serial, "start": s, "end": e, "zones": [], "system": {}, "idu": {}, "odu": {}}
        for z in zones:
            entity = z["entity"]
            out["zones"].append({
                "entity": entity, "name": z["name"],
                "numeric": {f: store.series(serial, entity, f, s, e) for f in ZONE_NUMERIC},
                "state": {f: store.series(serial, entity, f, s, e) for f in ZONE_STATE},
            })
        out["system"] = {
            "numeric": {f: store.series(serial, "system", f, s, e) for f in SYSTEM_NUMERIC},
            "state": {f: store.series(serial, "system", f, s, e) for f in SYSTEM_STATE},
        }
        for unit in ("idu", "odu"):
            out[unit] = {f: store.series(serial, unit, f, s, e) for f in UNIT_FIELDS}
        out["events"] = store.events(serial, s, e, 500)
        return out

    return app
