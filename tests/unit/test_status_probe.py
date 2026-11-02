"""The remote status probe, executed for real against a synthetic run directory.

This is the test that would have caught the CRLF bug: on Windows, piping the
script through a text-mode pipe rewrote every LF as CRLF, bash rejected the
script, the probe returned nothing and every run stayed "pending" forever.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rcf.core.errors import ExecutorError
from rcf.core.models import JobHandle
from rcf.core.runner import CommandResult, ScriptedRunner, SubprocessRunner
from rcf.executors.scripts import STATUS_PROBE
from rcf.executors.ssh import SshTransport
from rcf.resources import parsers


def probe(run_dir: Path, bash_binary: str) -> dict[str, str]:
    script = STATUS_PROBE.format(run_dir=str(run_dir).replace("\\", "/"))
    result = SubprocessRunner().run([bash_binary, "-s"], stdin=script, timeout=30)
    assert result.returncode == 0, result.stderr
    return parsers.parse_key_value_block(result.stdout)


def make_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    (run_dir / "run.log").write_text("hello\n", encoding="utf-8")
    return run_dir


def test_probe_reports_starting_before_the_pid_exists(tmp_path, bash_binary):
    data = probe(make_run_dir(tmp_path), bash_binary)
    assert data["state"] == "STARTING"


@pytest.mark.skipif(
    os.name == "nt",
    reason="MSYS `kill -0` cannot see processes outside its own session; "
    "the RUNNING branch is meaningful on the real POSIX targets",
)
def test_probe_reports_running_for_a_live_pid(tmp_path, bash_binary):
    run_dir = make_run_dir(tmp_path)
    # Use a process from the same (MSYS) world: on Windows, `kill -0` cannot see
    # native PIDs, so the test would otherwise check MSYS semantics instead of
    # the probe's.
    sleeper = subprocess.Popen([bash_binary, "-c", "sleep 30"])
    try:
        (run_dir / "pid").write_text(str(sleeper.pid), encoding="utf-8")
        data = probe(run_dir, bash_binary)
        assert data["state"] == "RUNNING", data
        assert data["pid"] == str(sleeper.pid)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_probe_reports_exited_with_code(tmp_path, bash_binary):
    run_dir = make_run_dir(tmp_path)
    (run_dir / "pid").write_text("999999", encoding="utf-8")
    (run_dir / "exit_code").write_text("0", encoding="utf-8")
    (run_dir / "started_at").write_text("2026-10-31T08:47:04Z", encoding="utf-8")
    data = probe(run_dir, bash_binary)
    assert data["state"] == "EXITED"
    assert data["exit_code"] == "0"
    assert data["started_at"] == "2026-10-31T08:47:04Z"


def test_probe_reports_lost_only_after_a_pid_existed(tmp_path, bash_binary):
    run_dir = make_run_dir(tmp_path)
    (run_dir / "pid").write_text("999999", encoding="utf-8")
    assert probe(run_dir, bash_binary)["state"] == "LOST"


def test_probe_reports_missing_directory(tmp_path, bash_binary):
    assert probe(tmp_path / "nope", bash_binary)["state"] == "MISSING"


def test_probe_survives_crlf_free_transport(tmp_path, bash_binary):
    """Belt and braces: the payload must not contain CR, whatever the platform."""

    script = STATUS_PROBE.format(run_dir="/tmp/x")
    assert "\r" not in script
    result = SubprocessRunner().run([bash_binary, "-c", "od -c"], stdin=script)
    assert "\\r" not in result.stdout


def _executor(stdout: str, returncode: int = 0):
    from rcf.core.models import NodeSpec
    from rcf.executors.ssh_exec import SshExecutor

    node = NodeSpec(id="ws", kind="ssh", ssh_alias="ws", work_root="/home/me/rcf")
    runner = ScriptedRunner([CommandResult(argv=(), returncode=returncode, stdout=stdout)])
    return SshExecutor(node, SshTransport("ws", runner))


def handle() -> JobHandle:
    return JobHandle(kind="ssh", node_id="ws", workdir="/home/me/rcf/runs/run_1")


def test_status_maps_starting_to_pending():
    status = _executor("state=STARTING\npid=\n").status(handle())
    assert status.state == "pending"
    assert "pid" in status.detail


def test_status_raises_when_the_probe_returns_nothing():
    """'Cannot observe' must not be reported as a state change."""

    with pytest.raises(ExecutorError, match="no state"):
        _executor("", returncode=2).status(handle())


def test_status_uses_the_jobs_own_end_time():
    """finished_at must be when the job ended, not when RCF happened to poll."""

    stdout = "state=EXITED\nexit_code=0\nfinished_at=2026-10-31T08:47:04Z\n"
    status = _executor(stdout).status(handle())
    assert status.state == "succeeded"
    assert status.finished_at is not None
    assert status.finished_at.year == 2026
    assert status.finished_at.hour == 8
    assert status.finished_at.minute == 47
    assert status.finished_at < status.observed_at


def test_malformed_finished_at_does_not_break_status():
    stdout = "state=EXITED\nexit_code=0\nfinished_at=not-a-date\n"
    status = _executor(stdout).status(handle())
    assert status.state == "succeeded"
    assert status.finished_at is None


def test_parse_find_manifest_handles_gnu_find_output():
    from rcf.executors.base import parse_find_manifest

    text = "1783\t1793437453.5\t./a32_results.json\n439574\t1793437444.7\t./n_traj.dat\nnoise\n"
    entries = parse_find_manifest(text)
    assert [e["path"] for e in entries] == ["a32_results.json", "n_traj.dat"]
    assert entries[0]["size"] == 1783
    assert entries[0]["modified_epoch"] == 1793437453.5


def test_manifest_paths_cannot_escape_the_destination(tmp_path):
    from rcf.core.errors import RcfError
    from rcf.runs.fetch import _safe_relative

    assert _safe_relative("./a/b.txt") == "a/b.txt"
    with pytest.raises(RcfError):
        _safe_relative("../etc/passwd")
    with pytest.raises(RcfError):
        _safe_relative("/etc/passwd")
    with pytest.raises(RcfError):
        _safe_relative("C:/Windows/system32")
