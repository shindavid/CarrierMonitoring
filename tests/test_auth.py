"""auth.py: password hashing, session tokens, client IP, home detection; user storage."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from carriermon.auth import (SESSION_REFRESH_AFTER, SESSION_TTL, HomeDetector, check_password, client_ip,
                             hash_password, load_secret, session_needs_refresh, sign_session, verify_session)
from carriermon.controldb import ControlStore


class TestPasswords:
    def test_roundtrip_and_salting(self):
        h1, s1 = hash_password("hunter2")
        h2, s2 = hash_password("hunter2")
        assert s1 != s2 and h1 != h2          # fresh salt each time
        assert check_password("hunter2", h1, s1) and check_password("hunter2", h2, s2)
        assert not check_password("hunter3", h1, s1)

    def test_explicit_salt_is_deterministic(self):
        assert hash_password("x", "00" * 16) == hash_password("x", "00" * 16)


class TestSecret:
    def test_created_once_then_reused(self, tmp_path: Path):
        path = tmp_path / "data" / "secret.key"
        a = load_secret(path)
        assert len(a) == 32 and path.exists() and (path.stat().st_mode & 0o777) == 0o600
        assert load_secret(path) == a


class TestSessions:
    secret = b"s" * 32

    def test_roundtrip(self):
        assert verify_session(self.secret, sign_session(self.secret, "nanny", "user")) == ("nanny", "user")

    def test_tampering_and_wrong_key(self):
        token = sign_session(self.secret, "nanny", "user")
        user, role, exp, sig = token.split("|")
        assert verify_session(self.secret, f"nanny|admin|{exp}|{sig}") is None   # role edited
        assert verify_session(b"t" * 32, token) is None
        assert verify_session(self.secret, "garbage") is None
        assert verify_session(self.secret, "") is None

    def test_expiry(self):
        assert verify_session(self.secret, sign_session(self.secret, "d", "admin", ttl=-1)) is None

    def test_unknown_role_rejected_even_if_signed(self):
        assert verify_session(self.secret, sign_session(self.secret, "d", "root")) is None

    def test_refresh_after_a_day_of_use(self):
        fresh = sign_session(self.secret, "d", "admin")
        assert not session_needs_refresh(self.secret, fresh)
        used = sign_session(self.secret, "d", "admin", ttl=SESSION_TTL - SESSION_REFRESH_AFTER - 1)
        assert session_needs_refresh(self.secret, used)
        assert not session_needs_refresh(self.secret, "garbage")
        assert not session_needs_refresh(self.secret, sign_session(self.secret, "d", "admin", ttl=-1))


class TestClientIp:
    def test_precedence(self):
        assert client_ip({"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"}, "127.0.0.1") == "1.2.3.4"
        assert client_ip({"x-forwarded-for": "5.6.7.8, 9.9.9.9"}, "127.0.0.1") == "5.6.7.8"
        assert client_ip({}, "192.168.1.9") == "192.168.1.9"
        assert client_ip({}, None) is None


class TestHomeDetector:
    def test_private_and_loopback_are_home(self):
        h = HomeDetector()
        for ip in ("10.1.2.3", "172.16.5.5", "192.168.0.1", "127.0.0.1", "::1"):
            assert h.at_home(ip), ip

    def test_listed_networks(self):
        h = HomeDetector(networks=("93.184.216.0/24", " 45.33.32.7 "))
        assert h.at_home("93.184.216.200") and h.at_home("45.33.32.7")
        assert not h.at_home("45.33.32.8")

    def test_matches_own_public_ipv4_with_caching(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return "ip=93.184.216.9\nloc=US\n"
        h = HomeDetector(public_ip_url="https://example/trace", fetch=fetch)
        assert h.at_home("93.184.216.9") and not h.at_home("93.184.216.10")
        assert h.public_ip() == "93.184.216.9"
        assert len(calls) == 1   # cached

    def test_ipv6_clients_match_on_the_64_prefix(self):
        """Phones on the home wifi arrive over IPv6 with their own rotating address in
        the ISP's delegated prefix; the server's own IPv6 identifies that prefix."""
        def fetch(url):
            return "ip=2601:241:8a00:165:e2c2:bab0:bb68:4bad" if "v6" in url else "73.44.64.145"
        h = HomeDetector(public_ip_url="v4", public_ip6_url="v6", fetch=fetch)
        assert h.at_home("2601:241:8a00:165:ec23:c726:9d87:799d")     # iPad, same /64
        assert h.at_home("73.44.64.145")
        assert not h.at_home("2601:241:8a00:166::1")                   # neighbouring prefix
        assert not h.at_home("73.44.64.146")
        assert h.public_ip() == "73.44.64.145"

    def test_one_protocol_failing_keeps_the_other(self):
        def fetch(url):
            if "v6" in url:
                raise OSError("no IPv6 route")
            return "93.184.216.9"
        h = HomeDetector(public_ip_url="v4", public_ip6_url="v6", fetch=fetch)
        assert h.at_home("93.184.216.9") and not h.at_home("2601::1")

    def test_bare_ip_response(self):
        h = HomeDetector(public_ip_url="u", fetch=lambda url: " 93.184.216.9\n")
        assert h.public_ip() == "93.184.216.9"

    def test_lookup_failure_keeps_last_value_and_backs_off(self):
        answers = iter(["93.184.216.9", RuntimeError("down"), RuntimeError("down")])
        def fetch(url):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        h = HomeDetector(public_ip_url="u", fetch=fetch, ttl=0)
        assert h.public_ip() == "93.184.216.9"
        assert h.public_ip() == "93.184.216.9"    # failure -> last known
        h._ts = 0
        assert h.at_home("93.184.216.9")           # still home on the cached value

    def test_no_url_means_no_lookup(self):
        h = HomeDetector(fetch=lambda url: (_ for _ in ()).throw(AssertionError("must not fetch")))
        assert h.public_ip() is None and not h.at_home("93.184.216.9")

    def test_ipv6_home_network_listing(self):
        h = HomeDetector(networks=("2601:241:8a00::/48",))
        assert h.at_home("2601:241:8a00:1::5") and not h.at_home("2601:241:8a01::5")

    def test_garbage_is_not_home(self):
        h = HomeDetector()
        assert not h.at_home(None) and not h.at_home("") and not h.at_home("testclient")


class TestUserStore:
    def test_add_verify_roles(self, control: ControlStore):
        assert not control.has_users()
        control.add_user("wife", "pw1", "user")
        control.add_user("david", "pw2", "admin")
        assert control.has_users()
        assert control.verify_user("wife", "pw1") == "user"
        assert control.verify_user("david", "pw2") == "admin"
        assert control.verify_user("wife", "pw2") is None
        assert control.verify_user("nobody", "pw1") is None
        assert [(u["name"], u["role"]) for u in control.list_users()] == [("david", "admin"), ("wife", "user")]

    def test_passwords_are_not_stored_in_clear(self, control: ControlStore):
        control.add_user("wife", "secret-pw", "user")
        row = control.conn.execute("SELECT pw_hash, salt FROM users").fetchone()
        assert "secret-pw" not in row["pw_hash"] and len(row["salt"]) == 32

    def test_passwd_remove_and_replace(self, control: ControlStore):
        control.add_user("wife", "old", "user")
        control.set_password("wife", "new")
        assert control.verify_user("wife", "old") is None and control.verify_user("wife", "new") == "user"
        control.add_user("wife", "again", "admin")     # add on an existing name replaces it
        assert control.verify_user("wife", "again") == "admin"
        control.remove_user("wife")
        assert not control.has_users()
        with pytest.raises(KeyError):
            control.remove_user("wife")
        with pytest.raises(KeyError):
            control.set_password("wife", "x")

    @pytest.mark.parametrize("name,password,role", [
        ("has space", "pw", "user"), ("", "pw", "user"), ("x" * 33, "pw", "user"),
        ("ok", "", "user"), ("ok", "pw", "root"),
    ])
    def test_validation(self, control: ControlStore, name, password, role):
        with pytest.raises(ValueError):
            control.add_user(name, password, role)

    def test_log_user_column_and_migration(self, control: ControlStore, tmp_path: Path):
        control.log("target", "x", user="nanny")
        control.log("check", "y")
        rows = control.recent_log(2)
        assert rows[0]["user"] is None and rows[1]["user"] == "nanny"
        # a file from before the column existed gets it added
        import sqlite3
        path = tmp_path / "old.sqlite"
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE control_log (id INTEGER PRIMARY KEY, ts REAL NOT NULL, event TEXT NOT NULL, "
                        "message TEXT NOT NULL, detail TEXT); INSERT INTO control_log(ts, event, message) VALUES (1, 'e', 'm');")
        c.commit(); c.close()
        assert ControlStore(path).recent_log(1)[0]["user"] is None
        assert time.time() > 0
