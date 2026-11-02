"""Real supervisor regression: shell operators must reach the shell unquoted."""
from __future__ import annotations

import time

from rcf.core.models import JobSpec, NodeSpec, PlannedCommand, ResourceRequirements
from rcf.core.runner import SubprocessRunner
from rcf.executors.local_exec import LocalExecutor


def test_formal_local_shell_executes_both_commands(tmp_path):
    executor = LocalExecutor(NodeSpec(id="local", kind="local"), tmp_path, SubprocessRunner())
    run_dir = tmp_path / "shell-run"
    receipt = executor.submit(JobSpec(
        run_id="shell-run", run_dir=str(run_dir), log_path=str(run_dir / "run.log"),
        command=PlannedCommand(argv=["echo FIRST && echo SECOND"], shell=True,
                               workdir=str(tmp_path)),
        resources=ResourceRequirements(),
    ))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = executor.status(receipt.handle)
        if status.state.terminal:
            break
        time.sleep(0.05)
    assert status.state.value == "succeeded", executor.logs(receipt.handle)
    assert status.exit_code == 0
    lines = (run_dir / "run.log").read_text(encoding="utf-8").splitlines()
    assert "FIRST" in [line.strip() for line in lines]
    assert "SECOND" in [line.strip() for line in lines]
