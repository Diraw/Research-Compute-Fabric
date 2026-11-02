from __future__ import annotations

from pathlib import Path

import pytest

from rcf.core.errors import SpecError
from rcf.progress.store import ProgressStore
from rcf.runs.store import Store


def test_observation_is_idempotent_detached_and_keeps_change_time(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    progress = ProgressStore(store)
    try:
        assert progress.latest("run-1") is None
        first = {"samples": [{"completed": 1}], "observed_at": "2026-11-01T00:00:00Z"}
        value = progress.save_observation("run-1", first, "rev-a")
        assert value["last_changed_at"] == first["observed_at"]
        again = progress.save_observation("run-1", {**first, "observed_at": "2026-11-01T00:01:00Z"}, "rev-a")
        assert again["last_changed_at"] == first["observed_at"]
        assert again["observed_at"] == "2026-11-01T00:01:00Z"
        assert store._conn.execute("SELECT COUNT(*) FROM progress_observations").fetchone()[0] == 1
        changed = progress.save_observation("run-1", {**first, "observed_at": "2026-11-01T00:02:00Z"}, "rev-b")
        assert changed["last_changed_at"] == changed["observed_at"]
        changed["samples"][0]["completed"] = 999
        assert progress.latest("run-1")["samples"][0]["completed"] == 1
        assert "revision" not in first
    finally:
        store.close()


def test_history_is_bounded_per_run_and_runs_schema_unchanged(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    before = list(store._conn.execute("PRAGMA table_info(runs)"))
    version = store._conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
    progress = ProgressStore(store)
    try:
        for i in range(150):
            progress.save_observation("run-1", {"value": i}, str(i))
        progress.save_observation("run-2", {"value": 0}, "0")
        assert store._conn.execute(
            "SELECT COUNT(*) FROM progress_observations WHERE run_id='run-1'",
        ).fetchone()[0] == 128
        assert progress.latest("run-1")["value"] == 149
        assert progress.latest("run-2")["value"] == 0
        recurring = progress.save_observation("run-1", {"value": 25}, "25")
        assert progress.latest("run-1") == recurring
        assert store._conn.execute(
            "SELECT revision FROM progress_observations WHERE run_id='run-1' ORDER BY seq DESC LIMIT 1",
        ).fetchone()[0] == "25"
        assert list(store._conn.execute("PRAGMA table_info(runs)")) == before
        assert store._conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == version
        assert store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        store.close()


def test_calibration_requires_verified_evidence_and_preserves_original(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    progress = ProgressStore(store)
    payload = {"evidence_verified": True, "run_id": "run-1", "seconds_per_unit": 4.0,
               "raw": {"signature": ["node", "env"], "method": "pilot"}}
    try:
        assert progress.calibration("key") is None
        for flag in (False, None, 1, "true"):
            with pytest.raises(SpecError, match="evidence_verified"):
                progress.save_calibration("key", "run-1", {**payload, "evidence_verified": flag})
        progress.save_calibration("key", "run-1", payload)
        assert progress.calibration("key") == payload
        assert progress.calibration("key") is not payload
        progress.save_calibration("key", "run-1", payload)
        assert store._conn.execute("SELECT COUNT(*) FROM progress_calibrations").fetchone()[0] == 1
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert ProgressStore(reopened).calibration("key") == payload
    finally:
        reopened.close()


def test_invalid_json_and_mismatched_run_are_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    progress = ProgressStore(store)
    try:
        for payload in ({"value": float("nan")}, {"run_id": "different"}, {"value": object()},
                        {"observed_at": "not-time"}, {"observed_at": "2026-11-01T00:00:00"}):
            with pytest.raises(SpecError):
                progress.save_observation("run-1", payload, "rev")
        with pytest.raises(SpecError):
            progress.save_calibration("key", "run-1", {"evidence_verified": True, "run_id": "different"})
        assert progress.latest("run-1") is None
    finally:
        store.close()
