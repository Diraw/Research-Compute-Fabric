"""Offline regressions for Windows launcher evidence and cancellation safety."""
from __future__ import annotations

import ast
import base64
import json
import shlex
import sys

import pytest
from test_windows_task_executor import decode_ps, executor, job_spec

from rcf.core.errors import ExecutorError
from rcf.core.models import JobHandle, RunState
from rcf.core.runner import CommandResult


def handle() -> JobHandle:
    return JobHandle(kind="windows_task", node_id="windows-worker", job_id="RCF-run_wsl_1",
                     workdir="/mnt/e/rcf/runs/run_wsl_1")


def ok(stdout: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(argv=(), returncode=returncode, stdout=stdout)


@pytest.mark.parametrize("state", ["STARTING", "MISSING", "UNKNOWN"])
def test_cancel_without_process_evidence_preserves_unknown(monkeypatch, state):
    ex, _ = executor()
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": state}, ok()))
    monkeypatch.setattr(ex, "_stop_task", lambda h: None)
    monkeypatch.setattr(ex, "_query_task", lambda h: None)
    monkeypatch.setattr(ex, "status", lambda h: type("S", (), {"state": RunState.UNKNOWN})())
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    assert ex.cancel(handle()).state is RunState.UNKNOWN
    assert not deleted


def test_stop_errors_are_not_silenced():
    ex, runner = executor([ok()])
    ex._stop_task(handle())
    ps = decode_ps(runner.inputs[0])
    assert "-ErrorAction SilentlyContinue" not in ps
    assert "$ErrorActionPreference = 'Stop'" in ps


@pytest.mark.parametrize("name", ["RCF-*", "RCF-[abc]", "RCF-a/b", "RCF-a\\b", "RCF-a\n"])
def test_ambiguous_task_names_rejected_before_query(name):
    ex, runner = executor()
    with pytest.raises(ExecutorError, match="task name"):
        ex._query_task(handle().model_copy(update={"job_id": name}))
    assert not runner.calls


@pytest.mark.parametrize("payload", [{}, {"exists": "false"}, [], {"exists": True},
                                      {"exists": True, "state": 3, "last_result": "oops"},
                                      {"exists": True, "state": 3, "last_result": None}])
def test_query_requires_explicit_valid_evidence(payload):
    ex, _ = executor([ok(json.dumps(payload))])
    with pytest.raises(ExecutorError):
        ex._query_task(handle())


def test_query_does_not_turn_powershell_failures_into_absence():
    ex, runner = executor([ok('{"exists":false}')])
    assert ex._query_task(handle()) is None
    ps = decode_ps(runner.inputs[0])
    assert "-ErrorAction SilentlyContinue" not in ps
    assert "TaskPath -eq '\\'" in ps and "TaskName -eq $name" in ps


def test_delete_checks_running_state_before_unregister():
    ex, runner = executor([ok()])
    ex._delete_task(handle())
    ps = decode_ps(runner.inputs[0])
    assert "Get-ScheduledTask" in ps
    assert ps.index("$task.State") < ps.index("Unregister-ScheduledTask")


@pytest.mark.parametrize("state", ["LOST", "MISSING"])
def test_uncertain_status_does_not_remove_task_evidence(monkeypatch, state):
    ex, _ = executor()
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": state}, ok()))
    monkeypatch.setattr(ex, "_query_task", lambda h: {"state": 4, "last_result": 0})
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    result = ex.status(handle())
    assert not result.state.terminal
    assert not deleted


def test_launcher_saves_errorlevel_before_echo_and_returns_it():
    ex, runner = executor([ok(), ok()])
    ex.submit(job_spec())
    import re

    encoded = re.findall(r"printf %s ([^ ]+) \| base64 -d", runner.inputs[0])
    launcher = next(text for item in encoded if "wsl.exe" in (text := base64.b64decode(item).decode()))
    assert 'set "rc=%ERRORLEVEL%"' in launcher
    assert "echo rc=%rc%" in launcher
    assert "exit /b %rc%" in launcher
    assert launcher.index('set "rc=') < launcher.index("echo rc=") < launcher.index("exit /b")


@pytest.mark.parametrize("task", [None, {"state": 3, "last_result": 0}])
def test_ready_task_uses_launcher_rc_even_when_scheduler_says_success(monkeypatch, task):
    ex, _ = executor([ok("rc=17\n")])
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "STARTING"}, ok()))
    monkeypatch.setattr(ex, "_query_task", lambda h: task)
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    result = ex.status(handle())
    assert result.state is RunState.FAILED
    assert result.exit_code == 17
    assert "launcher" in result.detail
    assert deleted


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16"])
@pytest.mark.parametrize("text", ["earlier\n中文启动失败\n最后一行\n", "中文\n错误\n"])
def test_fallback_reads_raw_bytes_and_tails_the_file(monkeypatch, tmp_path, encoding, text):
    ex, _ = executor()
    monkeypatch.setattr(ex, "exists", lambda h: True)
    directory = tmp_path / "run"
    directory.mkdir()
    logfile = directory / "rcf-task.log"
    logfile.write_bytes(text.encode(encoding))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return ok("RCF: run.log not found")
        # Execute only the generated Python body, not SSH, bash or Scheduler.
        tokens = shlex.split(command)
        assert tokens[:2] == ["python3", "-c"]
        ast.parse(tokens[2], feature_version=(3, 6))
        monkeypatch.setattr(sys, "argv", ["-c", *tokens[3:]])
        import contextlib
        import io

        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
        with contextlib.redirect_stdout(stream):
            exec(compile(tokens[2], "<remote-tail>", "exec"), {"__name__": "__main__"})
        return ok(output.getvalue().decode("utf-8"))

    monkeypatch.setattr(ex.transport, "run", run)
    result = ex.logs(handle().model_copy(update={"workdir": str(directory)}), tail=2)
    assert result == "".join(text.splitlines(keepends=True)[-2:])
    assert "rcf-task.log/run.log" not in commands[1]


@pytest.mark.parametrize("failure", ["stop", "query", "probe"])
def test_cancellation_operation_errors_keep_unknown_and_diagnostics(monkeypatch, failure):
    ex, _ = executor()
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "STARTING"}, ok()))
    monkeypatch.setattr(ex, "_stop_task", lambda h: None)
    monkeypatch.setattr(ex, "_query_task", lambda h: None)

    def fail(h):
        raise ExecutorError("permission denied")

    monkeypatch.setattr(ex, {"stop": "_stop_task", "query": "_query_task", "probe": "_probe"}[failure], fail)
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    result = ex.cancel(handle())
    assert result.state is RunState.UNKNOWN
    assert "permission denied" in result.detail
    assert not deleted


@pytest.mark.parametrize("task", [None, {"state": 0}, {"state": 2}, {"state": 3}, {"state": 4}])
@pytest.mark.parametrize("after", [{"state": "LOST", "pid": "123"},
                                    {"state": "STARTING"},
                                    {"state": "LOST", "pid": "456"},
                                    {"state": "RUNNING", "pid": "123", "pid_marker": "ok"}])
def test_cancellation_requires_both_scheduler_and_matching_wsl_evidence(monkeypatch, task, after):
    ex, _ = executor()
    probes = iter([{"state": "RUNNING", "pid": "123", "pid_marker": "ok"}, after])
    monkeypatch.setattr(ex, "_probe", lambda h: (next(probes), ok()))
    monkeypatch.setattr(ex.transport, "run", lambda *a, **kw: ok("cancelled=term\n"))
    monkeypatch.setattr(ex, "_stop_task", lambda h: None)
    monkeypatch.setattr(ex, "_query_task", lambda h: task)
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    confirmed = (task is None or task["state"] == 3) and after == {"state": "LOST", "pid": "123"}
    assert ex.cancel(handle()).state is (RunState.CANCELLED if confirmed else RunState.UNKNOWN)
    assert bool(deleted) is confirmed


@pytest.mark.parametrize("marker", ["mismatch", "unknown", ""])
def test_cancel_never_signals_an_unidentified_pid(monkeypatch, marker):
    ex, runner = executor()
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "RUNNING", "pid": "123", "pid_marker": marker}, ok()))
    monkeypatch.setattr(ex, "_stop_task", lambda h: None)
    monkeypatch.setattr(ex, "_query_task", lambda h: None)
    assert ex.cancel(handle()).state is RunState.UNKNOWN
    assert not runner.calls


def test_query_propagates_failure_and_stop_propagates_failure():
    ex, _ = executor([ok(returncode=1), ok(returncode=1)])
    with pytest.raises(ExecutorError):
        ex._query_task(handle())
    with pytest.raises(ExecutorError):
        ex._stop_task(handle())


@pytest.mark.parametrize("last_result", [0, 0x41303, 0x41301])
def test_scheduler_success_never_run_or_in_progress_without_receipt_stays_unknown(monkeypatch, last_result):
    ex, _ = executor([ok("RCF: file not found\n")])
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "STARTING"}, ok()))
    monkeypatch.setattr(ex, "_query_task", lambda h: {"state": 3, "last_result": last_result})
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    assert ex.status(handle()).state is RunState.UNKNOWN
    assert not deleted


@pytest.mark.parametrize("rc", [0, 1, -1, 4294967295])
def test_completed_launcher_without_run_receipt_is_bootstrap_failure(monkeypatch, rc):
    ex, _ = executor([ok(f"rc={rc}\n")])
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "STARTING"}, ok()))
    monkeypatch.setattr(ex, "_query_task", lambda h: {"state": 3, "last_result": 0})
    monkeypatch.setattr(ex, "_delete_task", lambda h: None)
    result = ex.status(handle())
    assert result.state is RunState.FAILED
    assert result.exit_code == rc


@pytest.mark.parametrize("stdout", ["rc=oops\n", "RCF: file not found\n"])
def test_malformed_or_missing_launcher_rc_does_not_make_false_terminal(monkeypatch, stdout):
    ex, _ = executor([ok(stdout)])
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "STARTING"}, ok()))
    monkeypatch.setattr(ex, "_query_task", lambda h: {"state": 3, "last_result": 0})
    assert ex.status(handle()).state is RunState.UNKNOWN


def test_scheduler_only_bootstrap_failure_keeps_its_only_diagnostic_receipt(monkeypatch):
    ex, _ = executor([ok("RCF: file not found\n")])
    monkeypatch.setattr(ex, "_probe", lambda h: ({"state": "STARTING"}, ok()))
    monkeypatch.setattr(ex, "_query_task", lambda h: {"state": 3, "last_result": 1})
    deleted = []
    monkeypatch.setattr(ex, "_delete_task", lambda h: deleted.append(h))
    assert ex.status(handle()).state is RunState.FAILED
    assert not deleted


def test_large_log_read_is_bounded(monkeypatch, tmp_path):
    ex, _ = executor()
    path = tmp_path / "log"
    path.write_bytes(b"x" * (3 * 1048576) + b"\nlast\n")
    tokens = shlex.split(ex._file_tail_command(str(path), 2))
    monkeypatch.setattr(sys, "argv", ["-c", *tokens[3:]])
    import contextlib
    import io

    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
    with contextlib.redirect_stdout(stream):
        exec(compile(tokens[2], "<remote-tail>", "exec"), {"__name__": "__main__"})
    assert output.getvalue() == b"last\n"
