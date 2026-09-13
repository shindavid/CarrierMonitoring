"""Logins, signed session cookies, and the "is this request from home?" check.

Accounts live in the control database (see ``ControlStore`` users methods) with
PBKDF2-hashed passwords and a role:
- ``admin``: may change controller settings from anywhere.
- ``user``:  may view from anywhere, but change settings only from the home network.

"Home" is decided from the client's IP. Requests through the Cloudflare tunnel carry
the real client address in ``CF-Connecting-IP``; on the home wifi that is the house's
public IP, which is also this server's own public IP — so the check is "does the
client's address equal ours?" (looked up every few minutes), plus any LAN ranges or
extra addresses listed in ``CARRIERMON_HOME_NETWORKS``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 30 * 86400
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
ROLES = ("admin", "user")


# ---------------------------------------------------------------- passwords
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex); a fresh random salt unless one is given."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def check_password(password: str, hash_hex: str, salt: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt)[0], hash_hex)


# ---------------------------------------------------------------- sessions
def load_secret(path: Path) -> bytes:
    """A random per-deployment key for signing session cookies, created on first use."""
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    path.write_text(secret.hex())
    path.chmod(0o600)
    return secret


def sign_session(secret: bytes, user: str, role: str, ttl: float = SESSION_TTL) -> str:
    body = f"{user}|{role}|{int(time.time() + ttl)}"
    return body + "|" + hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()


def verify_session(secret: bytes, token: str) -> tuple[str, str] | None:
    """(user, role) if the token is intact and unexpired, else None."""
    parts = token.split("|")
    if len(parts) != 4:
        return None
    user, role, exp, sig = parts
    body = f"{user}|{role}|{exp}"
    if not hmac.compare_digest(hmac.new(secret, body.encode(), hashlib.sha256).hexdigest(), sig):
        return None
    if not exp.isdigit() or int(exp) < time.time() or role not in ROLES:
        return None
    return user, role


# ---------------------------------------------------------------- where is the client?
def client_ip(headers: dict | object, peer: str | None) -> str | None:
    """The real client address: the tunnel's header first, then the socket peer.
    (The web server only listens on localhost behind the tunnel, so the header
    cannot be spoofed from outside; a LAN client spoofing it would only be
    claiming an address that is treated as home anyway.)"""
    get = headers.get  # works for a dict and for Starlette's Headers
    return get("cf-connecting-ip") or (get("x-forwarded-for") or "").split(",")[0].strip() or peer


def _fetch_public_ip(url: str) -> str:
    with urllib.request.urlopen(url, timeout=4) as resp:
        return resp.read().decode()


def _parse_ip(text: str) -> str:
    """Cloudflare's /cdn-cgi/trace is "key=value" lines; other services return a bare address."""
    for line in text.splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    return text.strip()


class HomeDetector:
    def __init__(self, networks: tuple[str, ...] | list[str] = (), public_ip_url: str | None = None,
                 ttl: float = 600, fetch: Callable[[str], str] = _fetch_public_ip) -> None:
        self.networks = [ipaddress.ip_network(n.strip(), strict=False) for n in networks if n and n.strip()]
        self.url = public_ip_url
        self.ttl = ttl
        self.fetch = fetch
        self._ip: str | None = None
        self._ts = 0.0

    def public_ip(self) -> str | None:
        """This server's public address, cached for ``ttl`` seconds; None if unknown."""
        if not self.url:
            return None
        if time.time() - self._ts < self.ttl:
            return self._ip
        self._ts = time.time()  # even on failure: don't hammer the service every request
        try:
            self._ip = str(ipaddress.ip_address(_parse_ip(self.fetch(self.url))))
        except Exception as exc:  # noqa: BLE001 - keep the last known value
            log.warning("public IP lookup failed (%s); keeping %s", exc, self._ip)
        return self._ip

    def at_home(self, ip: str | None) -> bool:
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.is_private or addr.is_loopback:
            return True
        if any(addr in net for net in self.networks):
            return True
        public = self.public_ip()
        return public is not None and str(addr) == public
