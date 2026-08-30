"""FastAPI dashboard + JSON API over the readings store."""

from __future__ import annotations

import base64
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

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
    store = Store(settings.db_path, read_only=True)
    app = FastAPI(title="carriermon")

    if settings.auth_user and settings.auth_password:
        expected = (settings.auth_user, settings.auth_password)

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            """HTTP Basic Auth on every route. TLS is terminated by the Cloudflare tunnel."""
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, expected[0]) and secrets.compare_digest(password, expected[1])
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Carrier Monitor", charset="UTF-8"'})
            return await call_next(request)

    def _range(start: float | None, end: float | None) -> tuple[float, float]:
        end = end or time.time()
        start = start or end - 86400
        return start, end

    @app.get("/")
    def index() -> Response:
        html = (STATIC / "index.html").read_text()
        if settings.dev:
            # Marks the page as the dev dashboard: amber chrome, "DEV" badge, tab title/favicon.
            html = html.replace('<html lang="en">', '<html lang="en" data-env="dev">', 1)
        return HTMLResponse(html)

    @app.get("/api/systems")
    def systems() -> list[dict]:
        # Only the serials are used (to populate the picker); zones come from
        # /api/dashboard. Computing zones here doubled every page load's cost.
        return [{"serial": s} for s in store.serials()]

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
