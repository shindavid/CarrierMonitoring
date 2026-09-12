"""FastAPI dashboard + JSON API over the readings store."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .controldb import ControlStore
from .db import Store
from .settings import Settings

STATIC = Path(__file__).parent / "static"

# Fields shown on the dashboard, per entity kind.
ZONE_NUMERIC = ["rt", "rh", "htsp", "clsp", "damperposition"]
ZONE_STATE = ["fan", "zoneconditioning", "hold", "currentActivity", "occupancy"]
SYSTEM_NUMERIC = ["oat", "filtrlvl", "humlvl"]
SYSTEM_STATE = ["mode", "humid"]
# inducerrpm (furnace combustion inducer) tells the dashboard when hot air is being
# delivered; odu opstat ("Stage N"/"dehumidify") tells it when cold air is.
UNIT_FIELDS = ["opstat", "opmode", "cfm", "blwrpm", "inducerrpm", "statpress", "type"]


class ControlEdit(BaseModel):
    enabled: bool | None = None
    target: float | None = Field(default=None, ge=55, le=85)


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path, read_only=True)
    # The controller's settings/state/log live in this checkout's own small database
    # (read-write even in dev — it is not the production readings file).
    control = ControlStore(settings.control_db_path)
    app = FastAPI(title="carriermon")

    if settings.auth_user and settings.auth_password:
        # Form-based login with a signed session cookie, rather than HTTP Basic Auth: the
        # browser-native Basic Auth popup isn't recognized by password managers (1Password),
        # whereas a real <form> login page is filled and saved like any other site login.
        expected = (settings.auth_user, settings.auth_password)
        COOKIE = "carriermon_session"
        # Signing key is derived from the credentials, so no extra secret to configure and
        # every stored cookie is invalidated automatically whenever the password changes.
        key = hashlib.sha256(f"{settings.auth_user}:{settings.auth_password}".encode()).digest()
        token = hmac.new(key, b"carriermon-session", hashlib.sha256).hexdigest()

        def authed(request: Request) -> bool:
            return hmac.compare_digest(request.cookies.get(COOKIE, ""), token)

        def login_page(error: str = "") -> HTMLResponse:
            html = (STATIC / "login.html").read_text().replace("{error}", error)
            if settings.dev:
                html = html.replace('<html lang="en">', '<html lang="en" data-env="dev">', 1)
            return HTMLResponse(html)

        @app.get("/login")
        def login_form(request: Request) -> Response:
            if authed(request):
                return RedirectResponse("/", status_code=303)
            return login_page()

        @app.post("/login")
        async def login_submit(request: Request) -> Response:
            body = urllib.parse.parse_qs((await request.body()).decode())
            user = body.get("username", [""])[0]
            password = body.get("password", [""])[0]
            ok = secrets.compare_digest(user, expected[0]) and secrets.compare_digest(password, expected[1])
            if not ok:
                return login_page("Incorrect username or password.")
            resp = RedirectResponse("/", status_code=303)
            # TLS is terminated by the Cloudflare tunnel, so the app only ever sees plain HTTP;
            # marking the cookie Secure here would stop the tunnel from forwarding it back.
            resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax")
            return resp

        @app.post("/logout")
        def logout() -> Response:
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(COOKIE)
            return resp

        @app.middleware("http")
        async def require_login(request: Request, call_next):
            if request.url.path in ("/login", "/logout") or authed(request):
                return await call_next(request)
            if request.url.path.startswith("/api/"):
                return Response("Authentication required", status_code=401)
            return RedirectResponse("/login", status_code=303)

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

    @app.get("/control")
    def control_page() -> Response:
        html = (STATIC / "control.html").read_text()
        if settings.dev:
            html = html.replace('<html lang="en">', '<html lang="en" data-env="dev">', 1)
        return HTMLResponse(html)

    def control_payload() -> dict:
        state = control.state()
        stale_after = settings.control_interval * 3
        state["loop_alive"] = bool(state["loop_alive_ts"]) and time.time() - state["loop_alive_ts"] < stale_after
        return {"settings": control.settings(), "state": state, "log": control.recent_log(300),
                "config": {"interval": settings.control_interval, "dev": settings.dev}}

    @app.get("/api/control")
    def control_get() -> dict:
        return control_payload()

    @app.post("/api/control")
    def control_set(edit: ControlEdit) -> dict:
        """Only two knobs: on/off and the target. Turning it on clears an override."""
        if edit.enabled is None and edit.target is None:
            raise HTTPException(400, "nothing to change")
        before = control.settings()
        control.update_settings(enabled=edit.enabled, target=edit.target)
        if edit.enabled is not None and edit.enabled != before["enabled"]:
            control.log("enabled" if edit.enabled else "disabled",
                        f"switched {'on' if edit.enabled else 'off'} from the control page")
        if edit.target is not None and edit.target != before["target"]:
            control.log("target", f"target changed {before['target']:g} → {edit.target:g}")
        return control_payload()

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
