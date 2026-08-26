"""Flatten Carrier payloads into (entity, field) -> value and diff against last known.

The flattening is deliberately generic so that *anything* Carrier adds to the
payload gets recorded; we only special-case zones (keyed by id) and a few
noisy bookkeeping fields.
"""

from __future__ import annotations

import time
from typing import Any

SKIP_FIELDS = {
    "timestamp", "utcTime", "localTime", "etag", "serverHasChanges", "ping",
    "pingRate", "__typename", "id",
}


def _coerce(value: Any) -> tuple[float | None, str | None]:
    """Return (value_num, value_text). Carrier sends most numbers as strings."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "on" if value else "off"
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        s = value.strip()
        try:
            return float(s), None
        except ValueError:
            return None, s
    return None, str(value)


def flatten(obj: Any, entity: str, prefix: str = "", out: dict | None = None) -> dict[tuple[str, str], Any]:
    """Recursively flatten a dict into {(entity, dotted.field): scalar}.

    Lists of dicts with an ``id`` become ``<prefix>:<id>`` sub-entities
    (so ``zones`` -> ``zone:1``); other lists are indexed.
    """
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in SKIP_FIELDS:
                continue
            if isinstance(value, dict):
                flatten(value, entity, f"{prefix}{key}.", out)
            elif isinstance(value, list):
                singular = key[:-3] + "y" if key.endswith("ies") else key[:-1] if key.endswith("s") else key
                for index, item in enumerate(value):
                    if isinstance(item, dict) and "id" in item:
                        sub_entity = f"{entity}.{singular}:{item['id']}" if entity not in ("system",) else f"{singular}:{item['id']}"
                        flatten(item, sub_entity, prefix, out)
                    else:
                        flatten(item, entity, f"{prefix}{key}[{index}].", out)
            else:
                out[(entity, f"{prefix}{key}")] = value
    else:
        out[(entity, prefix.rstrip("."))] = obj
    return out


def snapshot(status_raw: dict, config_raw: dict | None) -> dict[tuple[str, str], Any]:
    """Flatten a system's status (entity 'system'/'zone:N'/'idu'/'odu') and config ('config...')."""
    values: dict[tuple[str, str], Any] = {}
    status = dict(status_raw)
    for unit in ("idu", "odu"):
        unit_obj = status.pop(unit, None)
        if isinstance(unit_obj, dict):
            flatten(unit_obj, unit, "", values)
    flatten(status, "system", "", values)
    if config_raw:
        flatten(config_raw, "config", "", values)
    return values


def diff_rows(
    serial: str,
    values: dict[tuple[str, str], Any],
    last: dict[tuple[str, str], Any],
    source: str,
    force_all: bool,
    ts: float | None = None,
) -> tuple[list[tuple], int]:
    """Build readings rows; update ``last`` in place. Returns (rows, changed_count)."""
    ts = ts or time.time()
    rows: list[tuple] = []
    changed_count = 0
    for (entity, field), raw in values.items():
        num, text = _coerce(raw)
        stored = num if num is not None else text
        changed = (entity, field) not in last or last[(entity, field)] != stored
        if changed or force_all:
            rows.append((ts, serial, entity, field, num, text, int(changed), source))
        if changed:
            changed_count += 1
        last[(entity, field)] = stored
    return rows, changed_count
