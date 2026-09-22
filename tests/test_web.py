"""The FastAPI app: control API, pages, and the login gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carriermon.controldb import ControlStore
from carriermon.web import create_app

from conftest import make_settings, populate_readings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    populate_readings(tmp_path / "readings.sqlite")
    return TestClient(create_app(make_settings(tmp_path)))


@pytest.fixture
def authed(tmp_path: Path) -> TestClient:
    populate_readings(tmp_path / "readings.sqlite")
    app = create_app(make_settings(tmp_path, dev=False, auth_user="u", auth_password="p"))
    return TestClient(app, follow_redirects=False)


class TestControlApi:
    def test_get_shape(self, client: TestClient):
        d = client.get("/api/control").json()
        assert d["settings"]["enabled"] is False
        assert [z["name"] for z in d["zones"]] == ["Upstairs", "Downstairs"]
        assert d["zones"][0]["day_d"] == 70 and d["zones"][0]["night_start"] == "22:00"
        assert d["state"]["loop_alive"] is False and d["log"] == []
        assert d["config"] == {"interval": 60, "dev": True}

    def test_enable_and_disable_are_logged(self, client: TestClient):
        d = client.post("/api/control", json={"enabled": True}).json()
        assert d["settings"]["enabled"] is True
        assert d["log"][0]["event"] == "enabled"
        d = client.post("/api/control", json={"enabled": True}).json()   # no change -> no log
        assert len(d["log"]) == 1
        d = client.post("/api/control", json={"enabled": False}).json()
        assert d["log"][0]["event"] == "disabled"

    def test_zone_edit(self, client: TestClient):
        body = {"zones": {"zone:2": {"night_lo": 64, "night_d": 66, "night_hi": 70, "night_start": "21:00"}}}
        d = client.post("/api/control", json=body).json()
        z = next(z for z in d["zones"] if z["entity"] == "zone:2")
        assert (z["night_lo"], z["night_d"], z["night_hi"], z["night_start"]) == (64, 66, 70, "21:00")
        assert z["day_d"] == 70  # untouched
        msgs = {l["message"] for l in d["log"]}
        assert "Downstairs night: 70 (69–71) → 66 (64–70)" in msgs
        assert "Downstairs night starts 22:00 → 21:00" in msgs

    def test_unchanged_zone_edit_logs_nothing(self, client: TestClient):
        d = client.post("/api/control", json={"zones": {"zone:1": {"day_d": 70}}}).json()
        assert d["log"] == []

    def test_constraint_violation_is_422_with_message(self, client: TestClient):
        r = client.post("/api/control", json={"zones": {"zone:1": {"day_d": 75}}})
        assert r.status_code == 422 and r.json()["detail"] == "day desired temp must be within 69–71"
        r = client.post("/api/control", json={"zones": {"zone:1": {"day_lo": 70.5}}})
        assert r.status_code == 422 and "at least 2" in r.json()["detail"]

    def test_validation_of_values(self, client: TestClient):
        assert client.post("/api/control", json={"zones": {"zone:1": {"day_d": 100}}}).status_code == 422
        assert client.post("/api/control", json={"zones": {"zone:1": {"day_start": "25:00"}}}).status_code == 422
        assert client.post("/api/control", json={"zones": {"zone:1": {"day_start": "7:00"}}}).status_code == 422

    def test_unknown_zone_is_404(self, client: TestClient):
        r = client.post("/api/control", json={"zones": {"zone:9": {"day_d": 70}}})
        assert r.status_code == 404

    def test_empty_edit_is_400(self, client: TestClient):
        assert client.post("/api/control", json={}).status_code == 400
        assert client.post("/api/control", json={"zones": {}}).status_code == 400

    def test_enable_clears_override(self, client: TestClient, tmp_path: Path):
        ControlStore(tmp_path / "control.sqlite").trip_override("bumped")
        assert client.get("/api/control").json()["state"]["override"] == "bumped"
        d = client.post("/api/control", json={"enabled": True}).json()
        assert d["state"]["override"] is None


class TestPages:
    def test_control_page_marked_dev(self, client: TestClient):
        html = client.get("/control").text
        assert 'data-env="dev"' in html and "Carrier Control" in html and "zonecfg" in html

    def test_dashboard_links_to_control(self, client: TestClient):
        assert 'href="/control"' in client.get("/").text

    def test_dashboard_api(self, client: TestClient):
        d = client.get("/api/dashboard", params={"serial": "S1"}).json()
        assert [z["name"] for z in d["zones"]] == ["Upstairs", "Downstairs"]
        assert client.get("/api/dashboard", params={"serial": "nope"}).status_code == 404
        assert client.get("/api/systems").json() == [{"serial": "S1"}]


class TestAuth:
    def test_pages_redirect_and_api_401(self, authed: TestClient):
        r = authed.get("/control")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert authed.get("/api/control").status_code == 401
        assert authed.get("/login").status_code == 200

    def test_login_flow(self, authed: TestClient):
        r = authed.post("/login", data={"username": "u", "password": "wrong"})
        assert r.status_code == 200 and "Incorrect" in r.text
        r = authed.post("/login", data={"username": "u", "password": "p"})
        assert r.status_code == 303 and "carriermon_session" in r.cookies
        assert authed.get("/control").status_code == 200
        assert authed.get("/api/control").status_code == 200
        assert '<html lang="en" data-env="dev">' not in authed.get("/control").text
        r = authed.post("/logout")
        assert r.status_code == 303
        assert authed.get("/api/control").status_code == 401


@pytest.fixture
def accounts(tmp_path: Path) -> TestClient:
    """Two logins from the users table, no .env pair, a listed home network, no public-IP lookup."""
    populate_readings(tmp_path / "readings.sqlite")
    cs = ControlStore(tmp_path / "control.sqlite")
    cs.add_user("david", "pw-admin", "admin")
    cs.add_user("nanny", "pw-user", "user")
    app = create_app(make_settings(tmp_path, dev=False, home_networks=("93.184.216.0/24",)))
    return TestClient(app, follow_redirects=False)


def login(client: TestClient, user: str, password: str) -> None:
    r = client.post("/login", data={"username": user, "password": password})
    assert r.status_code == 303, r.text


class TestAccounts:
    def test_users_table_gates_the_site(self, accounts: TestClient):
        assert accounts.get("/control").status_code == 303
        assert accounts.get("/api/control").status_code == 401

    def test_wrong_password(self, accounts: TestClient):
        r = accounts.post("/login", data={"username": "nanny", "password": "nope"})
        assert r.status_code == 200 and "Incorrect" in r.text

    def test_me_at_home(self, accounts: TestClient):
        login(accounts, "nanny", "pw-user")
        me = accounts.get("/api/control", headers={"CF-Connecting-IP": "192.168.4.4"}).json()["me"]
        assert me == {"user": "nanny", "role": "user", "ip": "192.168.4.4", "at_home": True, "can_edit": True}

    def test_user_away_from_home_is_locked_out_entirely(self, accounts: TestClient):
        login(accounts, "nanny", "pw-user")
        away = {"CF-Connecting-IP": "8.8.8.8"}
        r = accounts.get("/api/control", headers=away)
        assert r.status_code == 403 and "home network" in r.json()["detail"]
        r = accounts.post("/api/control", json={"enabled": True}, headers=away)
        assert r.status_code == 403
        r = accounts.get("/control", headers=away)
        assert r.status_code == 403 and "home wifi" in r.text and "<form" in r.text
        assert accounts.get("/", headers=away).status_code == 403
        # the session survives: back at home, straight in
        assert accounts.get("/api/control", headers={"CF-Connecting-IP": "10.0.0.9"}).status_code == 200
        assert accounts.get("/api/control", headers={"CF-Connecting-IP": "10.0.0.9"}).json()["settings"]["enabled"] is False

    @pytest.mark.parametrize("ip", ["93.184.216.7", "192.168.1.20", "10.0.0.5", "127.0.0.1"])
    def test_user_at_home_can_edit_and_is_logged(self, accounts: TestClient, ip: str, tmp_path: Path):
        login(accounts, "nanny", "pw-user")
        r = accounts.post("/api/control", json={"enabled": True}, headers={"CF-Connecting-IP": ip})
        assert r.status_code == 200 and r.json()["me"]["at_home"] is True
        assert accounts.get("/control", headers={"CF-Connecting-IP": ip}).status_code == 200
        entry = ControlStore(tmp_path / "control.sqlite").recent_log(1)[0]
        assert entry["event"] == "enabled" and entry["user"] == "nanny"

    def test_admin_edits_from_anywhere(self, accounts: TestClient, tmp_path: Path):
        login(accounts, "david", "pw-admin")
        r = accounts.post("/api/control", json={"zones": {"zone:1": {"day_d": 71}}}, headers={"CF-Connecting-IP": "8.8.8.8"})
        assert r.status_code == 200 and r.json()["me"]["can_edit"] is True and r.json()["me"]["at_home"] is False
        entry = ControlStore(tmp_path / "control.sqlite").recent_log(1)[0]
        assert entry["message"].startswith("Upstairs day:") and entry["user"] == "david"

    def test_no_forwarding_header_uses_peer(self, accounts: TestClient):
        login(accounts, "david", "pw-admin")
        me = accounts.get("/api/control").json()["me"]
        assert me["ip"] == "testclient" and me["at_home"] is False   # not an address at all -> not home
        accounts.cookies.clear()
        login(accounts, "nanny", "pw-user")
        assert accounts.get("/api/control").status_code == 403

    def test_env_admin_and_table_users_coexist(self, tmp_path: Path):
        populate_readings(tmp_path / "readings.sqlite")
        ControlStore(tmp_path / "control.sqlite").add_user("wife", "pw", "user")
        app = create_app(make_settings(tmp_path, dev=False, auth_user="u", auth_password="p"))
        c = TestClient(app, follow_redirects=False)
        login(c, "u", "p")
        assert c.get("/api/control").json()["me"]["role"] == "admin"
        c.cookies.clear()
        login(c, "wife", "pw")
        assert c.get("/api/control").status_code == 403                       # a user, not at home
        assert c.get("/api/control", headers={"CF-Connecting-IP": "192.168.1.2"}).json()["me"]["role"] == "user"

    def test_session_survives_app_restart(self, tmp_path: Path):
        populate_readings(tmp_path / "readings.sqlite")
        ControlStore(tmp_path / "control.sqlite").add_user("d", "pw", "admin")
        s = make_settings(tmp_path, dev=False)
        c1 = TestClient(create_app(s), follow_redirects=False)
        login(c1, "d", "pw")
        cookie = c1.cookies["carriermon_session"]
        c2 = TestClient(create_app(s), follow_redirects=False)   # same secret file on disk
        assert c2.get("/api/control", cookies={"carriermon_session": cookie}).status_code == 200

    def test_open_server_is_admin_and_local(self, client: TestClient):
        me = client.get("/api/control").json()["me"]
        assert me["user"] is None and me["role"] == "admin" and me["can_edit"] is True


class TestPwaAssets:
    def test_manifest_and_icons_served(self, client: TestClient):
        m = client.get("/manifest.webmanifest")
        assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
        assert m.json()["start_url"] == "/control"
        for path in ("/sw.js", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            assert client.get(path).status_code == 200

    def test_assets_are_public_when_auth_is_on(self, tmp_path: Path):
        populate_readings(tmp_path / "readings.sqlite")
        ControlStore(tmp_path / "control.sqlite").add_user("d", "pw", "admin")
        c = TestClient(create_app(make_settings(tmp_path, dev=False)), follow_redirects=False)
        for path in ("/manifest.webmanifest", "/sw.js", "/icon-192.png", "/apple-touch-icon.png"):
            assert c.get(path).status_code == 200          # no login needed for install assets
        assert c.get("/control").status_code == 303        # the page itself still requires login


class TestPush:
    def test_config_off_without_keys(self, client: TestClient):
        assert client.get("/api/push/config").json() == {"enabled": False, "vapid_public_key": None}

    def test_config_on_with_keys(self, tmp_path: Path):
        populate_readings(tmp_path / "readings.sqlite")
        c = TestClient(create_app(make_settings(tmp_path, vapid_public_key="PUB", vapid_private_key="PRIV")))
        assert c.get("/api/push/config").json() == {"enabled": True, "vapid_public_key": "PUB"}

    def test_subscribe_stores_and_unsubscribe_removes(self, tmp_path: Path):
        populate_readings(tmp_path / "readings.sqlite")
        c = TestClient(create_app(make_settings(tmp_path)))
        sub = {"endpoint": "https://push/x", "keys": {"p256dh": "k", "auth": "a"}}
        assert c.post("/api/push/subscribe", json=sub).json() == {"ok": True}
        assert ControlStore(tmp_path / "control.sqlite").list_subscriptions()[0]["endpoint"] == "https://push/x"
        assert c.post("/api/push/unsubscribe", json={"endpoint": "https://push/x"}).json() == {"ok": True}
        assert ControlStore(tmp_path / "control.sqlite").list_subscriptions() == []

    def test_malformed_subscription_is_400(self, client: TestClient):
        assert client.post("/api/push/subscribe", json={"endpoint": "x", "keys": {}}).status_code == 400


class TestSessionLifetime:
    def test_cookie_is_long_lived_and_slides(self, tmp_path: Path):
        from carriermon.auth import SESSION_REFRESH_AFTER, SESSION_TTL, load_secret, sign_session
        populate_readings(tmp_path / "readings.sqlite")
        ControlStore(tmp_path / "control.sqlite").add_user("d", "pw", "admin")
        c = TestClient(create_app(make_settings(tmp_path, dev=False)), follow_redirects=False)
        r = c.post("/login", data={"username": "d", "password": "pw"})
        assert f"Max-Age={SESSION_TTL}" in r.headers["set-cookie"]
        # a fresh cookie is not re-issued on the next request
        assert "set-cookie" not in c.get("/api/control").headers
        # a cookie that has been in use for over a day is re-issued with a full lifetime
        secret = load_secret(tmp_path / "secret.key")
        old = sign_session(secret, "d", "admin", ttl=SESSION_TTL - SESSION_REFRESH_AFTER - 5)
        r = c.get("/api/control", cookies={"carriermon_session": old})
        assert r.status_code == 200 and f"Max-Age={SESSION_TTL}" in r.headers["set-cookie"]
        assert r.headers["set-cookie"].split(";")[0] != f"carriermon_session={old}"
