from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from rcf.progress import reporter
from rcf.progress.reporter import Reporter


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (*reporter._RANK_KEYS, "RCF_RUN_ID", "RCF_PROGRESS_FILE", "RCF_PROGRESS_TOTAL"):
        monkeypatch.delenv(key, raising=False)


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_environment_contract_and_session_restart(tmp_path, monkeypatch):
    path = tmp_path / "progress.jsonl"
    monkeypatch.setenv("RCF_PROGRESS_FILE", str(path))
    monkeypatch.setenv("RCF_RUN_ID", "run1")
    monkeypatch.setenv("RCF_PROGRESS_TOTAL", "12")
    with Reporter() as first:
        assert first.update(2, metric=0.03)
    with Reporter() as second:
        assert second.update(1)
    a, b = records(path)
    assert a["run_id"] == b["run_id"] == "run1"
    assert a["total"] == b["total"] == 12.0
    assert a["session_id"] != b["session_id"]
    assert a["seq"] == b["seq"] == 1
    assert a["elapsed_seconds"] >= 0
    assert a["timestamp"].endswith("Z")
    assert a["metric"] == 0.03
    assert not a["finished"]
    assert set(a) == {
        "run_id", "session_id", "seq", "timestamp", "elapsed_seconds", "phase",
        "completed", "total", "metric", "eta_reason", "finished",
    }


def test_throttle_heartbeat_uses_real_work_and_monotonic_clock(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reporter.time, "monotonic", lambda: clock[0])
    path = tmp_path / "progress.jsonl"
    writer = Reporter("run", path, total=100, min_interval_seconds=10)
    assert writer.update(5)
    clock[0] += 1
    assert not writer.update(7)
    assert not writer.heartbeat()
    clock[0] += 10
    assert writer.heartbeat()
    clock[0] += 10
    assert writer.heartbeat()
    data = records(path)
    assert [sample["completed"] for sample in data] == [5, 7, 7]
    assert [sample["seq"] for sample in data] == [1, 2, 3]
    assert [sample["elapsed_seconds"] for sample in data] == [0.0, 11.0, 21.0]


def test_adaptive_total_phase_reset_and_finished_bypass_throttle(tmp_path):
    path = tmp_path / "p.jsonl"
    writer = Reporter("run", path, total=10, min_interval_seconds=9999)
    assert writer.update(5)
    assert writer.update(6, total=20, force=True)
    assert writer.update(0, phase="postprocess")
    assert writer.update(1, total=1, finished=True)
    data = records(path)
    assert data[1]["total"] == 20
    assert data[2]["total"] is None
    assert data[2]["phase"] == "postprocess"
    assert data[3]["finished"] is True
    writer.close()
    assert len(records(path)) == 4
    with pytest.raises(ValueError, match="closed"):
        writer.heartbeat()


@pytest.mark.parametrize("key", reporter._RANK_KEYS)
def test_nonzero_mpi_rank_is_noop(tmp_path, monkeypatch, key):
    monkeypatch.setenv(key, "1")
    path = tmp_path / "p.jsonl"
    assert not Reporter("run", path).update(3, force=True)
    assert not path.exists()


def test_zero_rank_writes_and_missing_path_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "0")
    assert Reporter("run", tmp_path / "p.jsonl").update(0)
    assert not Reporter("run").heartbeat()


@pytest.mark.parametrize("field,value", [
    ("completed", float("nan")), ("completed", float("inf")), ("completed", -1),
    ("completed", True), ("completed", 10 ** 1000), ("total", -1), ("total", float("inf")),
    ("metric", float("nan")), ("metric", "1"), ("phase", ""),
    ("finished", "yes"), ("eta_reason", 1),
])
def test_rejects_invalid_numbers_and_fields_without_appending(tmp_path, field, value):
    path = tmp_path / "p.jsonl"
    writer = Reporter("run", path)
    kwargs = {"completed": 0, field: value}
    with pytest.raises(ValueError):
        writer.update(**kwargs)
    assert not path.exists()


def test_does_not_create_missing_parent_or_hide_write_failure(tmp_path):
    with pytest.raises(OSError):
        Reporter("run", tmp_path / "absent" / "p.jsonl").update(0)


def test_python36_syntax_and_standalone_import(tmp_path):
    source_path = Path(reporter.__file__)
    source = source_path.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 6))
    copied = tmp_path / "rcf_progress.py"
    copied.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("standalone_progress", copied)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.Reporter("standalone", tmp_path / "standalone.jsonl").update(2)
