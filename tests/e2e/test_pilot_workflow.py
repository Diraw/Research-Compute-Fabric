"""Real local pilot deadlines and sandbox-only SSH execution."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import FIXTURES, PYTHON, posix_fs, to_win

from rcf.core.models import (
    JobSpec,
    NodeSpec,
    PlannedCommand,
    ResourceRequirements,
    RunState,
    SshOptions,
)
from rcf.core.runner import SubprocessRunner
from rcf.executors.local_exec import LocalExecutor
from rcf.executors.ssh import SshTransport
from rcf.executors.ssh_exec import SshExecutor

pytestmark = pytest.mark.e2e


def wait_file(path: Path, timeout: float = 12) -> str:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.is_file():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    pytest.fail(f"pilot did not produce {path}")


def local_job(tmp_path: Path, argv: list[str], **command_args) -> JobSpec:
    return JobSpec(
        run_id="pilot_e2e", run_dir=str(tmp_path / "run"),
        command=PlannedCommand(argv=argv, workdir=str(tmp_path), **command_args),
        log_path=str(tmp_path / "run" / "run.log"), resources=ResourceRequirements(),
        metadata={"pilot_seconds": 2},
    )


def test_local_two_second_deadline_kills_child_and_preserves_progress(tmp_path):
    # Both processes stay alive, and on POSIX the child ignores TERM to prove
    # the watchdog escalates for the group after its leader has already exited.
    child = tmp_path / "child.py"
    child.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "if os.name != 'nt': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        "while True:\n"
        "    Path('heartbeat').write_text(str(time.monotonic()))\n"
        "    time.sleep(0.05)\n", encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import json, os, subprocess, sys, time\n"
        "flags = {'creationflags': 0x08000000} if os.name == 'nt' else {}\n"
        "subprocess.Popen([sys.executable, 'child.py'], **flags)\n"
        "with open(os.environ['RCF_PROGRESS_FILE'], 'w') as f:\n"
        "    f.write(json.dumps({'pilot': os.environ['RCF_PROGRESS_PILOT']}) + '\\n')\n"
        "print('pilot running', flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8",
    )
    ex = LocalExecutor(NodeSpec(id="local", kind="local"), tmp_path, SubprocessRunner())
    start = time.monotonic()
    receipt = ex.submit(local_job(tmp_path, [sys.executable, str(parent)]))
    run_dir = Path(receipt.handle.workdir)
    assert wait_file(run_dir / "exit_code").strip() == "124"
    assert time.monotonic() - start < 8
    report = json.loads(wait_file(run_dir / "pilot.json"))
    assert report["timeout_seconds"] == 2
    assert report["timed_out"] is True
    assert 1.9 <= report["elapsed_seconds"] < 7
    assert report["exit_code"] == 124
    assert wait_file(run_dir / "finished_at").strip().endswith("Z")
    assert json.loads(wait_file(run_dir / "progress.jsonl")) == {"pilot": "1"}
    assert "pilot running" in ex.logs(receipt.handle)
    status = ex.status(receipt.handle)
    assert status.state is RunState.FAILED and status.exit_code == 124
    child_pid = int(wait_file(tmp_path / "child.pid"))
    heartbeat = wait_file(tmp_path / "heartbeat")
    time.sleep(0.35)
    assert (tmp_path / "heartbeat").read_text(encoding="utf-8") == heartbeat
    if os.name == "nt":
        assert not LocalExecutor._alive(child_pid)
    else:
        # A killed orphan can remain as a zombie under a non-reaping container
        # PID 1; it is no longer executing, which is the watchdog guarantee.
        stat = Path(f"/proc/{child_pid}/stat")
        if stat.exists():
            assert stat.read_text().split()[2] == "Z"
        else:
            assert not LocalExecutor._alive(child_pid)


@pytest.mark.parametrize("child_seconds", [0.4, 4.0])
@pytest.mark.parametrize("generations", [1, 2])
def test_watchdog_tracks_descendants_after_immediate_parent_exit(tmp_path, child_seconds, generations):
    from rcf.progress.pilot import pilot_files

    child = tmp_path / "child.py"
    child.write_text(
        "import os, time\nfrom pathlib import Path\n"
        "Path('orphan.pid').write_text(str(os.getpid()))\n"
        "start = time.monotonic()\n"
        f"while time.monotonic() - start < {child_seconds}:\n"
        "    Path('orphan.heartbeat').write_text(str(time.monotonic()))\n"
        "    time.sleep(0.025)\n"
        "Path('orphan.finished').write_text('completed')\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import os, subprocess, sys\n"
        "flags = {'creationflags': 0x08000000} if os.name == 'nt' else {}\n"
        "subprocess.Popen([sys.executable, 'child.py'], **flags)\n",
        encoding="utf-8")
    if generations == 2:
        grandparent = tmp_path / "grandparent.py"
        grandparent.write_text(parent.read_text(encoding="utf-8").replace("child.py", "parent.py"),
                               encoding="utf-8")
        parent = grandparent
    command = PlannedCommand(argv=[sys.executable, str(parent)], workdir=str(tmp_path))
    for name, text in pilot_files(command, 2, posix_shell=False).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    start = time.monotonic()
    result = SubprocessRunner().run(
        [sys.executable, str(tmp_path / "rcf-pilot.py"), str(tmp_path / "pilot-args.json")],
        timeout=12)
    elapsed = time.monotonic() - start
    try:
        assert wait_file(tmp_path / "orphan.pid").strip().isdigit()
        report = json.loads(wait_file(tmp_path / "pilot.json"))
        if child_seconds > 2:
            assert result.returncode == 124, (report, result.stderr)
            assert report["timed_out"] is True
            assert 1.9 <= report["elapsed_seconds"] < 4
            heartbeat = wait_file(tmp_path / "orphan.heartbeat")
            time.sleep(0.2)
            assert (tmp_path / "orphan.heartbeat").read_text(encoding="utf-8") == heartbeat
            assert not (tmp_path / "orphan.finished").exists()
        else:
            assert result.returncode == 0 and report["timed_out"] is False
            assert elapsed >= child_seconds
            assert (tmp_path / "orphan.finished").read_text(encoding="utf-8") == "completed"
    finally:
        # The failing pre-fix reproduction is self-bounded and leaves no worker.
        time.sleep(max(0, child_seconds + 0.5 - (time.monotonic() - start)))


@pytest.mark.parametrize("exit_code", [0, 7])
def test_local_natural_completion_preserves_args_env_and_exit(tmp_path, exit_code):
    script = tmp_path / "quick.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps([sys.argv[1:], os.environ['PILOT_VALUE']]))\n"
        f"sys.exit({exit_code})\n", encoding="utf-8",
    )
    args = ["spaces and ; shell $tokens", "quote'\"", "a\nb"]
    spec = local_job(tmp_path, [sys.executable, str(script), *args], env={"PILOT_VALUE": "a'\nb"})
    ex = LocalExecutor(NodeSpec(id="local", kind="local"), tmp_path, SubprocessRunner())
    receipt = ex.submit(spec)
    run_dir = Path(receipt.handle.workdir)
    assert wait_file(run_dir / "exit_code").strip() == str(exit_code)
    report = json.loads(wait_file(run_dir / "pilot.json"))
    assert report["timed_out"] is False
    assert report["exit_code"] == exit_code
    assert json.dumps([args, "a'\nb"]) in ex.logs(receipt.handle)
    assert ex.status(receipt.handle).state is (
        RunState.SUCCEEDED if exit_code == 0 else RunState.FAILED)


def test_pilot_deadline_also_covers_environment_setup(tmp_path, bash_binary):
    spec = local_job(tmp_path, [sys.executable, "-c", "print('MUST NOT EXECUTE')"],
                     preamble=["sleep 30"])
    ex = LocalExecutor(NodeSpec(id="local", kind="local"), tmp_path, SubprocessRunner())
    receipt = ex.submit(spec)
    run_dir = Path(receipt.handle.workdir)
    assert wait_file(run_dir / "exit_code").strip() == "124"
    assert json.loads(wait_file(run_dir / "pilot.json"))["timed_out"] is True
    # The header records the reviewed command, so only reject its output line.
    assert "\nMUST NOT EXECUTE\n" not in ex.logs(receipt.handle)


@pytest.mark.parametrize("shell", [False, True])
def test_simulated_ssh_executes_uploaded_watchdog_with_preamble(ssh_sandbox, shell):
    sandbox = ssh_sandbox
    root = sandbox.host / "proj" / "hello"
    script = root / "pilot.py"
    script.write_text(
        "import os, sys, time\n"
        "print('setup=' + os.environ['SETUP_VALUE'], flush=True)\n"
        "print('arg=' + sys.argv[1], flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8",
    )
    node = NodeSpec(id="fake", kind="ssh", ssh_alias="lab-hpc",
                    work_root=posix_fs(sandbox.host / "rcf"),
                    ssh=SshOptions(program=[PYTHON, str(FIXTURES / "fake_ssh.py")]))
    ex = SshExecutor(node, SshTransport.from_node(node, SubprocessRunner()))
    run_dir = sandbox.host / "rcf" / "runs" / "pilot_ssh"
    command = PlannedCommand(
        argv=["python3", str(script), "literal; $(touch BAD)"] if not shell else [
            "python3", "pilot.py", "'literal; $(touch BAD)'"], shell=shell,
        workdir=str(root), env={"SETUP_VALUE": "explicit value"},
        preamble=["export SETUP_VALUE=overridden"],
    )
    receipt = ex.submit(JobSpec(run_id="pilot_ssh", run_dir=posix_fs(run_dir),
                               log_path=posix_fs(run_dir / "run.log"),
                               resources=ResourceRequirements(),
                               command=command, metadata={"pilot_seconds": 8}))
    assert wait_file(run_dir / "exit_code").strip() == "124"
    report = json.loads(wait_file(run_dir / "pilot.json"))
    assert report["timed_out"] is True
    assert report["timeout_seconds"] == 8
    assert "rcf-pilot.py" in wait_file(run_dir / "run.sh")
    assert json.loads(wait_file(run_dir / "pilot-args.json"))["argv"] == command.argv
    assert "setup=explicit value" in wait_file(run_dir / "run.log")
    assert "arg=literal; $(touch BAD)" in wait_file(run_dir / "run.log")
    assert not (root / "BAD").exists()
    assert ex.status(receipt.handle).exit_code == 124
    assert to_win(receipt.handle.workdir) == run_dir
