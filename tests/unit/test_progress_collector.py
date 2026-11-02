from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path

import pytest

from rcf.core.errors import SpecError
from rcf.core.models import NodeSpec
from rcf.core.runner import CommandResult, ScriptedRunner, SubprocessRunner
from rcf.progress.collector import _READ_SCRIPT, Collector


def sample(seq: int = 1, **changes: object) -> dict:
    return {
        "run_id": "run-1", "session_id": "session-1", "seq": seq,
        "timestamp": "2026-11-01T00:00:00Z", "elapsed_seconds": float(seq),
        "phase": "train", "completed": float(seq), "total": 100.0,
        "metric": None, "eta_reason": None, "finished": False, **changes,
    }


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "runs" / "run-1"
    directory.mkdir(parents=True)
    return directory


def collect(directory: Path, **kwargs: object) -> dict:
    return Collector(SubprocessRunner()).read(NodeSpec(id="local", kind="local"), str(directory),
                                               "run-1", **kwargs)


def write(directory: Path, records: list[dict], suffix: bytes = b"") -> None:
    (directory / "progress.jsonl").write_bytes(
        b"".join((json.dumps(value) + "\n").encode() for value in records) + suffix,
    )


def test_native_read_keeps_phase_finished_not_run_success(run_dir: Path) -> None:
    records = [sample(), sample(2, finished=True)]
    write(run_dir, records)
    result = collect(run_dir)
    assert result["samples"] == records
    assert result["complete"] is True
    assert result["truncated"] is False
    assert result["errors"] == []
    assert result["observed_at"].endswith("+00:00")
    assert "success" not in result


def test_tail_limits_are_explicit_and_tail_can_be_complete(run_dir: Path) -> None:
    records = [sample(i) for i in range(1, 8)]
    write(run_dir, records)
    size = len((json.dumps(records[-1]) + "\n").encode())
    result = collect(run_dir, max_bytes=size * 2 + 10, max_samples=1)
    assert result["samples"] == records[-1:]
    assert result["complete"] is True
    assert result["truncated"] is True
    assert any("byte limit" in error for error in result["errors"])
    assert any("sample limit" in error for error in result["errors"])


def test_partial_tail_preserves_complete_lines(run_dir: Path) -> None:
    write(run_dir, [sample()], b'{"run_id":')
    result = collect(run_dir)
    assert result["samples"] == [sample()]
    assert result["complete"] is False
    assert any("partial trailing" in error for error in result["errors"])
    (run_dir / "progress.jsonl").write_bytes(b"x" * 500)
    result = collect(run_dir, max_bytes=10)
    assert result["complete"] is False
    assert result["truncated"] is True
    assert not result["samples"]


@pytest.mark.parametrize("bad", [
    {"run_id": "other"}, {"session_id": ""}, {"seq": True}, {"seq": -1},
    {"timestamp": "2026-11-01T00:00:00"}, {"timestamp": "2026-02-31T00:00:00Z"},
    {"elapsed_seconds": float("inf")}, {"completed": -1}, {"total": float("nan")},
    {"metric": "1"}, {"finished": 1}, {"phase": ""}, {"eta_reason": {}},
])
def test_invalid_samples_are_not_silently_accepted(run_dir: Path, bad: dict) -> None:
    write(run_dir, [sample(**bad)])
    result = collect(run_dir)
    assert not result["samples"]
    assert result["errors"]
    assert result["complete"] is False


def test_session_sequences_and_elapsed_are_monotonic(run_dir: Path) -> None:
    write(run_dir, [sample(8), sample(9), sample(9), sample(10, elapsed_seconds=1),
                    sample(1, session_id="restart")])
    result = collect(run_dir)
    assert [value["seq"] for value in result["samples"]] == [8, 9, 1]
    assert len(result["errors"]) == 2
    assert not result["complete"]


def test_log_json_prefix_only_and_never_executes(run_dir: Path) -> None:
    marker = run_dir / "not-executed"
    (run_dir / "run.log").write_text(
        "ordinary output\n" + json.dumps(sample(5)) + "\n"
        + "RCF_PROGRESS " + json.dumps(sample()) + "\n"
        + f"RCF_PROGRESS __import__('pathlib').Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    result = collect(run_dir, source="log_json")
    assert result["samples"] == [sample()]
    assert not result["complete"]
    assert not marker.exists()


def test_missing_directory_regular_file_and_future_clock(run_dir: Path) -> None:
    assert "does not exist" in " ".join(collect(run_dir)["errors"])
    (run_dir / "progress.jsonl").mkdir()
    assert not collect(run_dir)["complete"]
    (run_dir / "progress.jsonl").rmdir()
    write(run_dir, [sample()])
    future = time.time() + 3600
    os.utime(run_dir / "progress.jsonl", (future, future))
    result = collect(run_dir)
    assert any("future" in error for error in result["errors"])
    assert not result["complete"]


def test_final_and_ancestor_links_are_blocked(run_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text(json.dumps(sample()) + "\n", encoding="utf-8")
    try:
        (run_dir / "progress.jsonl").symlink_to(outside / "data")
        alias = tmp_path / "linked"
        alias.symlink_to(run_dir.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert not collect(run_dir)["samples"]
    (run_dir / "progress.jsonl").unlink()
    write(run_dir, [sample()])
    result = collect(alias / "run-1")
    assert not result["samples"]
    assert result["errors"]
    assert (outside / "data").exists()


def test_project_directory_and_parent_traversal_rejected(run_dir: Path) -> None:
    result = collect(run_dir.parent)
    assert not result["samples"]
    assert any("exact run_id" in error for error in result["errors"])
    result = collect(run_dir / ".." / "run-1")
    assert any("parent traversal" in error for error in result["errors"])


def test_transport_timeout_invalid_response_and_bounds(run_dir: Path) -> None:
    runner = ScriptedRunner(responses=[CommandResult((), 1, timed_out=True),
                                       CommandResult((), 0, stdout="not JSON")])
    collector = Collector(runner)
    node = NodeSpec(id="local", kind="local")
    assert "timed out" in collector.read(node, str(run_dir), "run-1")["errors"][0]
    assert "invalid progress helper response" in collector.read(node, str(run_dir), "run-1")["errors"][0]
    for kwargs in ({"source": "arbitrary"}, {"max_bytes": 0}, {"max_samples": True},
                   {"timeout_seconds": float("nan")}):
        with pytest.raises(SpecError):
            collector.read(node, str(run_dir), "run-1", **kwargs)


def test_remote_helper_has_python36_grammar() -> None:
    ast.parse(_READ_SCRIPT, feature_version=(3, 6))


def test_duplicate_keys_invalid_utf8_and_deep_json_are_honest(run_dir: Path) -> None:
    path = run_dir / "progress.jsonl"
    path.write_text(json.dumps(sample())[:-1] + ', "seq": 2}\n', encoding="utf-8")
    result = collect(run_dir)
    assert any("duplicate JSON field" in error for error in result["errors"])
    assert not result["samples"]
    path.write_bytes(b"\xff\n")
    result = collect(run_dir)
    assert not result["complete"]
    assert result["errors"]
    path.write_text("[" * 2000 + "0" + "]" * 2000 + "\n", encoding="utf-8")
    result = collect(run_dir)
    assert not result["complete"]
    assert result["errors"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO unavailable on Windows")
def test_non_regular_fifo_is_not_read_or_blocked_forever(run_dir: Path) -> None:
    os.mkfifo(run_dir / "progress.jsonl")
    result = collect(run_dir, timeout_seconds=2)
    assert not result["complete"]
    assert any("regular file" in error for error in result["errors"])


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse safety")
def test_windows_ancestor_junction_is_blocked(run_dir: Path, tmp_path: Path) -> None:
    write(run_dir, [sample()])
    junction = tmp_path / "junction"
    runner = SubprocessRunner()
    result = runner.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(run_dir.parent)])
    assert result.ok, result.stderr
    try:
        observation = collect(junction / "run-1")
        assert not observation["complete"]
        assert not observation["samples"]
        assert any("reparse" in error for error in observation["errors"])
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_permission_failure_is_not_empty_success(run_dir: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses ordinary file permissions")
    write(run_dir, [sample()])
    path = run_dir / "progress.jsonl"
    path.chmod(0)
    try:
        result = collect(run_dir)
        assert not result["complete"]
        assert any("permission denied" in error for error in result["errors"])
    finally:
        path.chmod(0o600)


@pytest.mark.skipif(
    not os.name == "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="execute-only ancestor permissions require a non-root POSIX user",
)
def test_execute_only_ancestor_is_traversed_without_read_permission(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    directory = locked / "runs" / "run-1"
    directory.mkdir(parents=True)
    write(directory, [sample(1), sample(2)])
    os.chmod(locked, 0o111)
    try:
        result = collect(directory)
    finally:
        os.chmod(locked, 0o755)
    assert result["errors"] == []
    assert [item["seq"] for item in result["samples"]] == [1, 2]
