"""Load configuration from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments). Does not override existing env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    username: str | None
    password: str | None
    dev: bool
    db_path: Path
    poll_seconds: int
    retention_days: int
    web_host: str
    web_port: int
    auth_user: str | None
    auth_password: str | None

    def require_carrier_login(self) -> None:
        if not self.username or not self.password:
            raise SystemExit("CARRIER_USERNAME / CARRIER_PASSWORD not set (copy .env.example to .env)")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            username=os.environ.get("CARRIER_USERNAME") or None,
            password=os.environ.get("CARRIER_PASSWORD") or None,
            dev=os.environ.get("CARRIERMON_DEV", "") == "1",
            db_path=Path(os.environ.get("CARRIERMON_DB", "data/carriermon.sqlite")),
            poll_seconds=int(os.environ.get("CARRIERMON_POLL_SECONDS", "300")),
            retention_days=int(os.environ.get("CARRIERMON_RETENTION_DAYS", "7")),
            web_host=os.environ.get("CARRIERMON_HOST", "0.0.0.0"),
            web_port=int(os.environ.get("CARRIERMON_PORT", "8471")),
            auth_user=os.environ.get("CARRIERMON_AUTH_USER") or None,
            auth_password=os.environ.get("CARRIERMON_AUTH_PASSWORD") or None,
        )
