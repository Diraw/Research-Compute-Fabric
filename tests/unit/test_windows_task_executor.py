"""Windows Task Scheduler executor: no session-bound child process."""

from __future__ import annotations

import base64
import json

import pytest

from rcf.core.models import (
    JobHandle,
    JobSpec,
    NodeKind,
    NodeSpec,
    PlannedCommand,
    ResourceRequirements,
    RunState,
    WindowsTaskSpec,
)
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.executors.ssh import SshTransport
from rcf.executors.windows_task import WindowsTaskExecutor


def node(**overrides) -> NodeSpec:
    payload = {
        "id": "windows-worker",
        "kind": NodeKind.SSH,
        "ssh_alias": "windows-worker",
        "work_root": "/mnt/e/rcf",
        "scp_path_map": {"/mnt/e": "E:/"},
        "windows_task": WindowsTaskSpec(distro="Ubuntu"),
    }
    payload.update(overrides)
    return NodeSpec(**payload)


def executor(responses: list[CommandResult | Exception] | None = None):
    runner = ScriptedRunner(responses or [])
    return WindowsTaskExecutor(node(), SshTransport("windows-worker", runner)), runner


def job_spec() -> JobSpec:
    return JobSpec(
        run_id="run_wsl_1",
        run_dir="/mnt/e/rcf/runs/run_wsl_1",
        command=PlannedCommand(argv=["python3", "-m", "train"], workdir="/mnt/e/rcf/proj"),
        log_path="/mnt/e/rcf/runs/run_wsl_1/run.log",
        resources=ResourceRequirements(),
        metadata={"params": {"seed": 1}},
    )


def decode_ps(stdin: str | None) -> str:
    assert stdin
    marker = "-EncodedCommand "
    encoded = stdin.split(marker, 1)[1].split()[0]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_windows_task_distro_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match="letters, digits"):
        WindowsTaskSpec(distro="Ubuntu 24.04")


def test_windows_task_requires_ssh_and_absolute_wsl_root():
    with pytest.raises(ValueError, match="kind = 'ssh'"):
        NodeSpec(id="x", kind=NodeKind.LOCAL, windows_task=WindowsTaskSpec())
    with pytest.raises(ValueError, match="absolute WSL work_root"):
        NodeSpec(
            id="x", kind=NodeKind.SSH, ssh_alias="x", work_root="~/rcf",
            windows_task=WindowsTaskSpec(),
        )


def test_nested_windows_task_block_loads_and_selects_the_executor():
    parsed = NodeSpec.model_validate({
        "id": "windows-worker", "kind": "ssh", "ssh_alias": "windows-worker", "work_root": "/mnt/e/rcf",
        "windows_task": {"distro": "Ubuntu", "task_prefix": "RCF-", "windows_work_root": "E:/rcf"},
    })
    assert parsed.windows_task is not None
    assert parsed.executor_kind == "windows_task"

    from rcf.executors.factory import build_executor

    built = build_executor(None, parsed, ScriptedRunner())  # config is unused for SSH
    assert isinstance(built, WindowsTaskExecutor)


def test_submit_writes_launcher_and_registers_a_task_without_a_trigger():
    ex, runner = executor([CommandResult(argv=(), returncode=0), CommandResult(argv=(), returncode=0)])
    receipt = ex.submit(job_spec())

    assert receipt.state is RunState.PENDING
    assert receipt.handle.kind == "windows_task"
    assert receipt.handle.job_id == "RCF-run_wsl_1"
    assert len(runner.calls) == 2
    upload = runner.inputs[0]
    assert upload and "rcf-task.sh" in upload and "rcf-task.cmd" in upload
    assert "run.sh" in upload and "run.json" in upload
    assert ex._windows_path("/mnt/e/rcf/runs/run_wsl_1") == "E:\\rcf\\runs\\run_wsl_1"

    ps = decode_ps(runner.inputs[1])
    assert "New-ScheduledTaskAction" in ps
    assert "Register-ScheduledTask" in ps
    assert "Start-ScheduledTask" in ps
    assert "cmd.exe" in ps and "rcf-task.cmd" in ps
    assert "New-ScheduledTaskTrigger" not in ps  # manual start only, never a second fire

    import re

    payloads = re.findall(r"printf %s ([^ ]+) \| base64 -d", upload)
    decoded = [base64.b64decode(item).decode("utf-8") for item in payloads]
    launcher = next(item for item in decoded if "rcf-task.cmd" not in item and "wsl.exe" in item)
    assert 'wsl.exe' in launcher
    assert '-d Ubuntu' in launcher
    assert '-d "Ubuntu"' not in launcher
    assert '/mnt/e/rcf/runs/run_wsl_1/rcf-task.sh' in launcher
    assert '%ERRORLEVEL%' in launcher


def test_status_reads_exit_code_and_cleans_up_task():
    ex, _ = executor([
        CommandResult(argv=(), returncode=0, stdout="state=EXITED\nexit_code=0\nfinished_at=2026-11-01T00:00:00Z\n"),
        CommandResult(argv=(), returncode=0),
    ])
    status = ex.status(JobHandle(kind="windows_task", node_id="windows-worker", job_id="RCF-run_wsl_1",
                                 workdir="/mnt/e/rcf/runs/run_wsl_1"))
    assert status.state is RunState.SUCCEEDED
    assert status.exit_code == 0


def test_starting_task_is_pending_until_run_sh_writes_its_pid():
    ex, _ = executor([
        CommandResult(argv=(), returncode=0, stdout="state=STARTING\n"),
        CommandResult(argv=(), returncode=0,
                      stdout=json.dumps({"exists": True, "state": 4, "last_result": 267009})),
    ])
    status = ex.status(JobHandle(kind="windows_task", node_id="windows-worker", job_id="RCF-run_wsl_1",
                                 workdir="/mnt/e/rcf/runs/run_wsl_1"))
    assert status.state is RunState.PENDING
    assert "queued/running" in status.detail


def test_task_that_finished_without_pid_is_failed_with_windows_code():
    ex, _ = executor([
        CommandResult(argv=(), returncode=0, stdout="state=STARTING\n"),
        CommandResult(argv=(), returncode=0,
                      stdout=json.dumps({"exists": True, "state": 3, "last_result": 1})),
        CommandResult(argv=(), returncode=0),
    ])
    status = ex.status(JobHandle(kind="windows_task", node_id="windows-worker", job_id="RCF-run_wsl_1",
                                 workdir="/mnt/e/rcf/runs/run_wsl_1"))
    assert status.state is RunState.FAILED
    assert "Windows result 1" in status.detail


def test_cancel_kills_wsl_process_then_stops_and_deletes_task(monkeypatch):
    ex, _ = executor()
    probes = iter([
        {"state": "RUNNING", "pid": "123", "pid_marker": "ok"},
        {"state": "LOST", "pid": "123"},
    ])
    monkeypatch.setattr(ex, "_probe", lambda handle: (next(probes), CommandResult(argv=(), returncode=0)))
    monkeypatch.setattr(ex, "_query_task", lambda handle: {"state": 3, "last_result": 0})
    stopped, deleted = [], []
    monkeypatch.setattr(ex, "_stop_task", lambda handle: stopped.append(handle.job_id))
    monkeypatch.setattr(ex, "_delete_task", lambda handle: deleted.append(handle.job_id))
    monkeypatch.setattr(
        ex.transport, "run",
        lambda command, timeout=None: CommandResult(argv=(), returncode=0, stdout="cancelled=term\nstate=CANCELLED\n"),
    )
    result = ex.cancel(JobHandle(kind="windows_task", node_id="windows-worker", job_id="RCF-run_wsl_1",
                                 workdir="/mnt/e/rcf/runs/run_wsl_1"))
    assert result.state is RunState.CANCELLED
    assert stopped == ["RCF-run_wsl_1"]
    assert deleted == ["RCF-run_wsl_1"]


def test_logs_fall_back_to_task_launcher_log(monkeypatch):
    ex, runner = executor([
        CommandResult(argv=(), returncode=0, stdout="RCF: run.log not found"),
        CommandResult(argv=(), returncode=0, stdout="wsl: failed to start\n"),
    ])
    monkeypatch.setattr(ex, "exists", lambda handle: True)
    text = ex.logs(JobHandle(kind="windows_task", node_id="windows-worker", job_id="RCF-run_wsl_1",
                             workdir="/mnt/e/rcf/runs/run_wsl_1"))
    assert "wsl: failed to start" in text
    assert len(runner.calls) == 2
