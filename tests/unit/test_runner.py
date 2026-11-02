"""Process launching: console windows must never appear for the user.

Regression for a real bug: on Windows 11 with a ConPTY-hosted parent
(git-bash / Electron), a child created with ``DETACHED_PROCESS`` gets a *visible*
console window. During a local run that window popped up and stole focus.
``CREATE_NO_WINDOW`` gives the child an invisible console instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from bash_support import find_test_bash

from rcf.core.runner import SubprocessRunner, spawn_detached

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows console semantics")


def test_subprocess_runner_hides_console(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    if os.name == "nt":
        import _winapi

        real_create = _winapi.CreateProcess

        def spy(*args, **kwargs):
            captured["creationflags"] = args[5]
            return real_create(*args, **kwargs)

        monkeypatch.setattr(_winapi, "CreateProcess", spy)
    else:
        real_run = subprocess.run

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
    result = SubprocessRunner().run([sys.executable, "-c", "print('ok')"])
    assert result.ok
    if os.name == "nt":
        assert int(captured["creationflags"]) & CREATE_NO_WINDOW
        assert not int(captured["creationflags"]) & DETACHED_PROCESS


def test_runner_accepts_pathlike_working_directory(tmp_path):
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path,
    )
    assert result.ok, result.stderr
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


@windows_only
def test_spawn_detached_child_has_no_console_window(tmp_path):
    probe = tmp_path / "hwnd.txt"
    code = (
        "import ctypes, pathlib;"
        f"pathlib.Path(r'{probe}').write_text("
        "str(ctypes.windll.kernel32.GetConsoleWindow()))"
    )
    spawn_detached([sys.executable, "-c", code], cwd=str(tmp_path))
    deadline = time.time() + 15
    while time.time() < deadline and not probe.exists():
        time.sleep(0.05)
    assert probe.exists(), "detached child never ran"
    assert probe.read_text().strip() == "0", "child got a console window"


@windows_only
def test_spawn_detached_does_not_use_detached_process(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    spawn_detached([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    flags = int(captured.get("creationflags", 0))
    assert flags & CREATE_NO_WINDOW
    assert not flags & DETACHED_PROCESS


def test_spawn_detached_runs_in_its_own_session(tmp_path):
    marker = Path(tmp_path) / "ran.txt"
    code = f"import pathlib; pathlib.Path(r'{marker}').write_text('yes')"
    pid = spawn_detached([sys.executable, "-c", code], cwd=str(tmp_path))
    assert pid > 0
    deadline = time.time() + 15
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.read_text() == "yes"


@windows_only
def test_timeout_does_not_wait_for_descendant_output_pipes(tmp_path):
    """The parent is killed at .2s; inherited stdout must not add two seconds."""
    child = tmp_path / "child.py"
    child.write_text("import time; time.sleep(2)", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "print('before-timeout', flush=True)\n"
        "print('error-before-timeout', file=sys.stderr, flush=True)\n"
        "subprocess.Popen([sys.executable, sys.argv[1]], stdout=sys.stdout, stderr=sys.stderr, creationflags=0x08000000)\n"
        "time.sleep(2)\n", encoding="utf-8",
    )
    started = time.monotonic()
    result = SubprocessRunner().run([sys.executable, str(parent), str(child)], timeout=0.2)
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert result.returncode == 124
    assert "before-timeout" in result.stdout
    assert "error-before-timeout" in result.stderr
    assert elapsed < 1.0, f".2s timeout took {elapsed:.3f}s waiting for child pipe EOF"
    assert result.duration_s < 1.0


@windows_only
def test_managed_tree_cleanup_is_handle_scoped(tmp_path):
    """A timed-out tree is reaped without touching an unrelated process."""
    import _winapi

    script = tmp_path / "tree.py"
    child_pid = tmp_path / "child.pid"
    grandchild_pid = tmp_path / "grandchild.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'], "
        "stdout=sys.stdout, stderr=sys.stderr, creationflags=0x08000000)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(3)\n", encoding="utf-8",
    )
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3]], "
        "stdout=sys.stdout, stderr=sys.stderr, creationflags=0x08000000)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "print('spawned', flush=True)\n"
        "time.sleep(3)\n", encoding="utf-8",
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"],
                                 creationflags=CREATE_NO_WINDOW)
    try:
        result = SubprocessRunner().run(
            [sys.executable, str(script), str(child_pid), str(child_script), str(grandchild_pid)],
            timeout=.5,
        )
        assert result.timed_out
        assert result.returncode == 124
        assert "spawned" in result.stdout
        assert unrelated.poll() is None, "cleanup must not target a separate process"
        for pid_file in (child_pid, grandchild_pid):
            pid = int(pid_file.read_text())
            try:
                handle = _winapi.OpenProcess(0x100000 | 0x1000, False, pid)
            except OSError as exc:
                assert exc.winerror == 87, "only a disappeared PID is acceptable"
            else:
                try:
                    assert _winapi.WaitForSingleObject(handle, 500) == _winapi.WAIT_OBJECT_0
                finally:
                    _winapi.CloseHandle(handle)
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)


@windows_only
def test_managed_runner_has_no_console_and_preserves_binary_input():
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import ctypes, sys; "
         "assert ctypes.windll.kernel32.GetConsoleWindow() == 0; "
         "sys.stdout.buffer.write(sys.stdin.buffer.read()); "
         "sys.stderr.write('stderr')"], stdin="a\nb\n", env={"RCF_TEST": "yes"},
    )
    assert result.ok
    assert result.stdout == "a\nb\n"
    assert result.stderr == "stderr"


@windows_only
def test_spawn_detached_survives_managed_launcher_exit(tmp_path):
    marker = tmp_path / "survived"
    child = tmp_path / "detached.py"
    child.write_text("import pathlib, sys, time; time.sleep(.5); "
                     "pathlib.Path(sys.argv[1]).write_text('survived')", encoding="utf-8")
    # A persistent supervisor is launched outside the managed short-command
    # path. Its creator can exit without a job-handle close killing the job.
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "from rcf.core.runner import spawn_detached\nimport sys\n"
        "spawn_detached([sys.executable, sys.argv[1], sys.argv[2]])\n", encoding="utf-8",
    )
    completed = SubprocessRunner().run(
        [sys.executable, str(launcher), str(child), str(marker)], timeout=3,
    )
    assert completed.ok
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert marker.read_text() == "survived"


def test_missing_executable_is_reported(tmp_path):
    from rcf.core.errors import ExecutableMissing

    with pytest.raises(ExecutableMissing):
        SubprocessRunner().run(["definitely-not-a-real-binary-xyz"])


def test_stdin_is_sent_byte_for_byte_without_crlf():
    """Windows text mode would rewrite "\n" as "\r\n" and break remote bash."""

    bash = find_test_bash()
    if bash is None:
        pytest.skip("Git Bash unavailable")
    result = SubprocessRunner().run([bash, "-c", "od -c"], stdin="a\nb\n")
    assert result.ok
    assert "\r" not in result.stdout, result.stdout


def test_multiline_shell_script_survives_the_pipe():
    script = "if [ -d / ]; then\n  echo yes\nfi\n"
    bash = find_test_bash()
    if bash is None:
        pytest.skip("Git Bash unavailable")
    result = SubprocessRunner().run([bash, "-s"], stdin=script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "yes"
