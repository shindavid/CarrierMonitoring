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
