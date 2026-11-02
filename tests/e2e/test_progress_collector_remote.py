from __future__ import annotations

import json

import pytest
from conftest import FIXTURES, PYTHON, Sandbox

from rcf.core.models import NodeSpec, SshOptions
from rcf.core.runner import SubprocessRunner
from rcf.progress.collector import Collector

pytestmark = pytest.mark.e2e


def node() -> NodeSpec:
    return NodeSpec(id="lab-hpc", kind="ssh", ssh_alias="lab-hpc",
                    ssh=SshOptions(program=[PYTHON, str(FIXTURES / "fake_ssh.py")]))


def record(seq: int) -> dict:
    return {"run_id": "run-1", "session_id": "s", "seq": seq,
            "timestamp": "2026-11-01T00:00:00Z", "elapsed_seconds": float(seq),
            "phase": "main", "completed": float(seq), "total": 10.0,
            "metric": None, "eta_reason": None, "finished": False}


def test_fake_ssh_reads_exact_bounded_file_with_literal_paths(ssh_sandbox: Sandbox) -> None:
    root = ssh_sandbox.host / "rcf test's ;$()" / "runs" / "run-1"
    root.mkdir(parents=True)
    records = [record(i) for i in range(1, 10)]
    (root / "progress.jsonl").write_text(
        "".join(json.dumps(value) + "\n" for value in records), encoding="utf-8",
    )
    collector = Collector(SubprocessRunner())
    result = collector.read(node(), str(root), "run-1", max_bytes=800, max_samples=2)
    assert result["samples"] == records[-2:]
    assert result["complete"] is True
    assert result["truncated"] is True
    assert any("limit" in error for error in result["errors"])


def test_fake_ssh_log_prefix_partial_and_mismatch_are_honest(ssh_sandbox: Sandbox) -> None:
    root = ssh_sandbox.run_dirs / "run-1"
    root.mkdir()
    (root / "run.log").write_text(
        "hello\n" + "RCF_PROGRESS " + json.dumps(record(1)) + "\n"
        + "RCF_PROGRESS " + json.dumps({**record(2), "run_id": "other"}) + "\n"
        + 'RCF_PROGRESS {"partial":', encoding="utf-8",
    )
    result = Collector(SubprocessRunner()).read(node(), str(root), "run-1", source="log_json")
    assert result["samples"] == [record(1)]
    assert not result["complete"]
    assert any("partial trailing" in error for error in result["errors"])
    assert any("run_id mismatch" in error for error in result["errors"])


def test_fake_ssh_missing_and_transport_errors_are_not_empty_success(
    ssh_sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ssh_sandbox.run_dirs / "run-1"
    root.mkdir()
    collector = Collector(SubprocessRunner())
    result = collector.read(node(), str(root), "run-1")
    assert not result["complete"]
    assert any("does not exist" in error for error in result["errors"])
    monkeypatch.setenv("RCF_FAKE_SSH_MODE", "auth")
    result = collector.read(node(), str(root), "run-1")
    assert not result["complete"]
    assert any("auth_failed" in error for error in result["errors"])


def test_fake_ssh_ancestor_link_is_not_followed(ssh_sandbox: Sandbox) -> None:
    root = ssh_sandbox.run_dirs / "run-1"
    root.mkdir()
    (root / "progress.jsonl").write_text(json.dumps(record(1)) + "\n", encoding="utf-8")
    link = ssh_sandbox.host / "linked-runs"
    try:
        link.symlink_to(ssh_sandbox.run_dirs, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    result = Collector(SubprocessRunner()).read(node(), str(link / "run-1"), "run-1")
    assert not result["samples"]
    assert not result["complete"]
    assert result["errors"]
