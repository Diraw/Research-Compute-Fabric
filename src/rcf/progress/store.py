"""Bounded JSON-only progress companion tables sharing the run Store connection."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..core.errors import SpecError, StoreError
from ..core.ids import iso, now_utc
from ..runs.store import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, revision TEXT NOT NULL, payload TEXT NOT NULL,
    UNIQUE(run_id, revision)
);
CREATE INDEX IF NOT EXISTS progress_ix_observations_run
    ON progress_observations(run_id, seq DESC);
CREATE TABLE IF NOT EXISTS progress_latest (
    run_id TEXT PRIMARY KEY, revision TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_calibrations (
    key TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
);
"""


def _dump(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise SpecError("progress payload must be a JSON object")
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise SpecError(f"invalid progress JSON: {exc}") from exc


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SpecError(f"progress {name} must be a nonempty bounded string")


class ProgressStore:
    """No filesystem/network work occurs within these short SQLite transactions.

    Repeated meaningful revisions refresh observed_at but keep last_changed_at.
    Returned values are detached JSON copies; history retains at most 128/run.
    Calibration is admitted only after the service verifies terminal evidence.
    """

    def __init__(self, store: Store) -> None:
        self._conn = store._conn
        self._lock = store._lock
        try:
            with self._lock, self._conn:
                self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot initialize progress tables: {exc}") from exc

    def save_observation(self, run_id: str, payload: dict[str, Any], revision: str) -> dict[str, Any]:
        _identifier(run_id, "run_id")
        _identifier(revision, "revision")
        value = json.loads(_dump(payload))
        if value.get("run_id", run_id) != run_id:
            raise SpecError("progress observation run_id mismatch")
        value["run_id"] = run_id
        value["revision"] = revision
        value.setdefault("observed_at", iso(now_utc()))
        try:
            observed = value["observed_at"]
            if not isinstance(observed, str) or "T" not in observed:
                raise ValueError("not an ISO timestamp")
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                raise ValueError("not UTC")
        except ValueError as exc:
            raise SpecError("progress observed_at must be an ISO UTC string") from exc
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT revision,payload FROM progress_latest WHERE run_id=?", (run_id,),
                ).fetchone()
                previous = json.loads(row["payload"]) if row else None
                value["last_changed_at"] = (
                    previous["last_changed_at"] if row and row["revision"] == revision
                    else value["observed_at"]
                )
                encoded = _dump(value)
                # A previously seen revision can recur after a different one.
                # Move that transition to the recent end, not on ordinary polls.
                if row and row["revision"] != revision:
                    self._conn.execute(
                        "DELETE FROM progress_observations WHERE run_id=? AND revision=?", (run_id, revision),
                    )
                self._conn.execute(
                    "INSERT INTO progress_observations(run_id,revision,payload) VALUES (?,?,?) "
                    "ON CONFLICT(run_id,revision) DO UPDATE SET payload=excluded.payload",
                    (run_id, revision, encoded),
                )
                self._conn.execute(
                    "INSERT INTO progress_latest(run_id,revision,payload) VALUES (?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                    (run_id, revision, encoded),
                )
                self._conn.execute(
                    "DELETE FROM progress_observations WHERE run_id=? AND seq NOT IN "
                    "(SELECT seq FROM progress_observations WHERE run_id=? ORDER BY seq DESC LIMIT 128)",
                    (run_id, run_id),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot save progress observation: {exc}") from exc
        return value

    def latest(self, run_id: str) -> dict[str, Any] | None:
        _identifier(run_id, "run_id")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload FROM progress_latest WHERE run_id=?", (run_id,),
                ).fetchone()
            return json.loads(row["payload"]) if row else None
        except sqlite3.Error as exc:
            raise StoreError(f"cannot read progress observation: {exc}") from exc

    def save_calibration(self, key: str, run_id: str, payload: dict[str, Any]) -> None:
        _identifier(key, "calibration key")
        _identifier(run_id, "run_id")
        encoded = _dump(payload)
        if payload.get("evidence_verified") is not True:
            raise SpecError("progress calibration requires evidence_verified=true")
        if payload.get("run_id", run_id) != run_id:
            raise SpecError("progress calibration run_id mismatch")
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO progress_calibrations(key,run_id,payload) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET run_id=excluded.run_id,payload=excluded.payload",
                    (key, run_id, encoded),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot save progress calibration: {exc}") from exc

    def calibration(self, key: str) -> dict[str, Any] | None:
        _identifier(key, "calibration key")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload FROM progress_calibrations WHERE key=?", (key,),
                ).fetchone()
            return json.loads(row["payload"]) if row else None
        except sqlite3.Error as exc:
            raise StoreError(f"cannot read progress calibration: {exc}") from exc
