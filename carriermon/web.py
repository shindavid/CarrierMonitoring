"""FastAPI dashboard + JSON API over the readings store."""

from __future__ import annotations

import secrets
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .auth import SESSION_TTL, HomeDetector, client_ip, load_secret, sign_session, verify_session
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


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class ZoneEdit(BaseModel):
    day_lo: float | None = Field(default=None, ge=55, le=85)
    day_d: float | None = Field(default=None, ge=55, le=85)
    day_hi: float | None = Field(default=None, ge=55, le=85)
    night_lo: float | None = Field(default=None, ge=55, le=85)
    night_d: float | None = Field(default=None, ge=55, le=85)
    night_hi: float | None = Field(default=None, ge=55, le=85)
    day_start: str | None = Field(default=None, pattern=HHMM)
    night_start: str | None = Field(default=None, pattern=HHMM)


class ControlEdit(BaseModel):
    enabled: bool | None = None
    zones: dict[str, ZoneEdit] | None = None   # entity -> partial edit


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path, read_only=True)
    # The controller's settings/state/log live in this checkout's own small database
    # (read-write even in dev — it is not the production readings file).
    control = ControlStore(settings.control_db_path)
    app = FastAPI(title="carriermon")

    # -- logins ------------------------------------------------------------------
    # Form login with a signed session cookie (password managers fill a real <form>;
    # the browser's Basic Auth popup they don't). Accounts come from the users table
    # (`carriermon user add`), plus the CARRIERMON_AUTH_USER/PASSWORD pair from .env as
    # an admin so an existing deployment keeps working. With neither, the server is
    # open (local dev use).
    secret = load_secret(settings.control_db_path.parent / "secret.key")
    home = HomeDetector(settings.home_networks, settings.public_ip_url)
    env_admin = (settings.auth_user, settings.auth_password) if settings.auth_user and settings.auth_password else None
    COOKIE = "carriermon_session"

    def auth_enabled() -> bool:
        return env_admin is not None or control.has_users()

    def login_page(error: str = "") -> HTMLResponse:
        html = (STATIC / "login.html").read_text().replace("{error}", error)
        if settings.dev:
            html = html.replace('<html lang="en">', '<html lang="en" data-env="dev">', 1)
        return HTMLResponse(html)

    @app.get("/login")
    def login_form(request: Request) -> Response:
        if verify_session(secret, request.cookies.get(COOKIE, "")):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        body = urllib.parse.parse_qs((await request.body()).decode())
        user = body.get("username", [""])[0]
        password = body.get("password", [""])[0]
        role = None
        if env_admin and secrets.compare_digest(user, env_admin[0]) and secrets.compare_digest(password, env_admin[1]):
            role = "admin"
        else:
            role = control.verify_user(user, password)
        if role is None:
            return login_page("Incorrect username or password.")
        resp = RedirectResponse("/", status_code=303)
        # TLS is terminated by the Cloudflare tunnel, so the app only ever sees plain HTTP;
        # marking the cookie Secure here would stop the tunnel from forwarding it back.
        resp.set_cookie(COOKIE, sign_session(secret, user, role), max_age=SESSION_TTL, httponly=True, samesite="lax")
        return resp

    @app.post("/logout")
    def logout() -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    AWAY = "Standard logins only work from the home network. Log in as an admin, or connect to the home wifi."

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        """Every request: must be logged in, and a standard user must be at home — for
        pages and API alike. Admins pass from anywhere."""
        request.state.user, request.state.role = None, "admin"   # open server: everyone is admin
        if auth_enabled() and request.url.path not in ("/login", "/logout"):
            api = request.url.path.startswith("/api/")
            session = verify_session(secret, request.cookies.get(COOKIE, ""))
            if session is None:
                return Response("Authentication required", status_code=401) if api \
                    else RedirectResponse("/login", status_code=303)
            request.state.user, request.state.role = session
            if session[1] != "admin" and not home.at_home(request_ip(request)):
                # Keep the cookie: back on the home wifi they are simply in again.
                return JSONResponse({"detail": AWAY}, status_code=403) if api \
                    else HTMLResponse(login_page(AWAY).body, status_code=403)
        return await call_next(request)

    def request_ip(request: Request) -> str | None:
        return client_ip(request.headers, request.client.host if request.client else None)

    def whoami(request: Request) -> dict:
        ip = request_ip(request)
        at_home = home.at_home(ip)
        role = request.state.role
        return {"user": request.state.user, "role": role, "ip": ip, "at_home": at_home,
                "can_edit": role == "admin" or at_home}

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

    def control_zones() -> list[dict]:
        """Enabled zones (from the readings) with their control ranges."""
        serials = store.serials()
        zones = store.zones(serials[0]) if serials else []
        ranges = control.zones([z["entity"] for z in zones])
        return [{"entity": z["entity"], "name": z["name"], **ranges[z["entity"]]} for z in zones]

    def control_payload() -> dict:
        state = control.state()
        stale_after = settings.control_interval * 3
        state["loop_alive"] = bool(state["loop_alive_ts"]) and time.time() - state["loop_alive_ts"] < stale_after
        return {"settings": control.settings(), "zones": control_zones(), "state": state,
                "log": control.recent_log(300),
                "config": {"interval": settings.control_interval, "dev": settings.dev}}

    @app.get("/api/control")
    def control_get(request: Request) -> dict:
        return {**control_payload(), "me": whoami(request)}

    @app.post("/api/control")
    def control_set(edit: ControlEdit, request: Request) -> dict:
        """On/off, and per-zone day/night ranges and start times. Turning it on clears
        an override. Standard users may only change things from the home network."""
        me = whoami(request)
        if not me["can_edit"]:
            raise HTTPException(403, "Changes are allowed only from the home network for standard users.")
        who = me["user"] or "local"
        if edit.enabled is None and not edit.zones:
            raise HTTPException(400, "nothing to change")
        if edit.enabled is not None:
            before = control.settings()["enabled"]
            control.set_enabled(edit.enabled)
            if edit.enabled != before:
                control.log("enabled" if edit.enabled else "disabled",
                            f"switched {'on' if edit.enabled else 'off'} from the control page", user=who)
        names = {z["entity"]: z["name"] for z in control_zones()}
        for entity, zedit in (edit.zones or {}).items():
            if entity not in names:
                raise HTTPException(404, f"unknown zone {entity}")
            fields = zedit.model_dump(exclude_none=True)
            if not fields:
                continue
            before = control.zone(entity)
            try:
                after = control.update_zone(entity, **fields)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            for period in ("day", "night"):
                keys = (f"{period}_lo", f"{period}_d", f"{period}_hi")
                if tuple(before[k] for k in keys) != tuple(after[k] for k in keys):
                    fmt = lambda z: f"{z[keys[1]]:g} ({z[keys[0]]:g}–{z[keys[2]]:g})"  # noqa: E731
                    control.log("target", f"{names[entity]} {period}: {fmt(before)} → {fmt(after)}", user=who)
                st = f"{period}_start"
                if before[st] != after[st]:
                    control.log("target", f"{names[entity]} {period} starts {before[st]} → {after[st]}", user=who)
        return {**control_payload(), "me": me}

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
