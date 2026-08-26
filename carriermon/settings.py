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
    username: str
    password: str
    db_path: Path
    poll_seconds: int
    web_host: str
    web_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        username = os.environ.get("CARRIER_USERNAME")
        password = os.environ.get("CARRIER_PASSWORD")
        if not username or not password:
            raise SystemExit(
                "CARRIER_USERNAME / CARRIER_PASSWORD not set (copy .env.example to .env)"
            )
        return cls(
            username=username,
            password=password,
            db_path=Path(os.environ.get("CARRIERMON_DB", "data/carriermon.sqlite")),
            poll_seconds=int(os.environ.get("CARRIERMON_POLL_SECONDS", "300")),
            web_host=os.environ.get("CARRIERMON_HOST", "0.0.0.0"),
            web_port=int(os.environ.get("CARRIERMON_PORT", "8471")),
        )
