"""Identifier and fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def new_id(prefix: str) -> str:
    """Sortable, human-readable id: ``run_20261031T153245Z_a1b2c3``."""

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


def stable_hash(payload: Any, *, length: int = 16) -> str:
    """Stable content hash for plans/environments (key order independent)."""

    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:length]


def short_job_name(run_id: str) -> str:
    """Slurm job names are limited to 128 chars; keep it compact and traceable."""

    return run_id.replace("_", "-")[:64]
