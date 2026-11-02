"""Pilot construction checks; no scheduler or real remote hosts."""

from __future__ import annotations

import ast
import base64
import json
import re

import pytest

from rcf.core.errors import ExecutorError
from rcf.core.models import (
    JobSpec,
    NodeSpec,
    PlannedCommand,
    ResourceRequirements,
    SlurmSpec,
    WindowsTaskSpec,
)
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.executors.scripts import run_script
from rcf.executors.slurm_exec import SlurmExecutor, build_sbatch_script
from rcf.executors.ssh import SshTransport
from rcf.executors.ssh_exec import SshExecutor
from rcf.executors.windows_task import WindowsTaskExecutor
from rcf.progress.pilot import PILOT_SCRIPT, pilot_files, pilot_interpreter, pilot_timeout


def job() -> JobSpec:
    return JobSpec(
        run_id="pilot_unit", run_dir="/mnt/e/rcf/runs/pilot_unit",
        command=PlannedCommand(
            argv=["mpirun", "-np", "4", "python3", "train.py", "a; touch BAD", "x'\ny"],
            workdir="/mnt/e/project", env={"SECRET": "a'\nb"},
            preamble=["export SETUP_OK=yes"],
        ),
        log_path="/mnt/e/rcf/runs/pilot_unit/run.log", resources=ResourceRequirements(),
        metadata={"pilot_seconds": 2},
    )


def decoded_upload(text: str) -> list[str]:
    return [base64.b64decode(item).decode("utf-8") for item in
            re.findall(r"printf %s ([^ ]+) \| base64 -d", text)]


def test_pilot_interpreter_skips_mpi_launchers_and_prefers_resolved_python():
    assert pilot_interpreter(["mpirun", "-np", "4", "python3", "train.py"]) == "python3"
    assert pilot_interpreter(["/public/envs/sci/bin/python3.12", "train.py"]) == (
        "/public/envs/sci/bin/python3.12")
    assert pilot_interpreter(["srun", "--ntasks=4", "/opt/py/bin/python", "x.py"]) == (
        "/opt/py/bin/python")
    assert pilot_interpreter(["mpirun", "-np", "4", "./solver"]) is None


def test_slurm_pilot_uses_the_resolved_interpreter_path():
    spec = job()
    spec.command.argv = ["mpirun", "-np", "4", "/public/envs/sci/bin/python3", "train.py"]
    script = build_sbatch_script(job=spec, run_dir=spec.run_dir, slurm=SlurmSpec())
    assert "/public/envs/sci/bin/python3 \"$RUN_DIR/rcf-pilot.py\"" in script
    assert "mpirun \"$RUN_DIR/rcf-pilot.py\"" not in script


def test_standalone_script_is_python36_syntax_and_zero_rcf_imports():
    tree = ast.parse(PILOT_SCRIPT, feature_version=(3, 6))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(not isinstance(node, ast.ImportFrom) for node in imports)
    assert not any(alias.name.startswith("rcf") for node in imports for alias in node.names)


@pytest.mark.parametrize("value", [True, 0, 301, "2", 2.0, -1])
def test_invalid_pilot_metadata_never_becomes_unlimited(value):
    with pytest.raises(ExecutorError):
        pilot_timeout({"pilot_seconds": value})


def test_payload_preserves_launcher_args_environment_shell_and_preamble():
    spec = job()
    files = pilot_files(spec.command, 2)
    payload = json.loads(files["pilot-args.json"])
    assert payload["argv"] == spec.command.argv
    assert payload["env"] == spec.command.env
    assert payload["preamble"] == spec.command.preamble
    assert payload["shell"] is False
    assert files["rcf-pilot.py"] == PILOT_SCRIPT
    assert pilot_files(spec.command, None) == {}


def test_run_script_wraps_once_and_never_interpolates_command_into_execution():
    spec = job()
    text = run_script(run_id=spec.run_id, run_dir=spec.run_dir, command=spec.command,
                      workdir=spec.command.workdir, pilot_seconds=2)
    assert text.count('python3 "$RUN_DIR/rcf-pilot.py" "$RUN_DIR/pilot-args.json"') == 1
    assert "export SETUP_OK=yes" not in text
    assert "export SECRET=" not in text
    assert "mpirun" not in next(line for line in text.splitlines() if "rcf-pilot.py" in line)


@pytest.mark.parametrize("kind", ["ssh", "slurm", "windows_task"])
def test_all_remote_executors_upload_script_and_json_without_real_submission(kind):
    spec = job()
    node = NodeSpec(id="fake", kind="slurm" if kind == "slurm" else "ssh",
                    ssh_alias="fake", work_root="/mnt/e/rcf",
                    slurm=SlurmSpec() if kind == "slurm" else None,
                    windows_task=WindowsTaskSpec(distro="Ubuntu", windows_work_root="E:/rcf")
                    if kind == "windows_task" else None)
    responses = [CommandResult(argv=(), returncode=0, stdout="rcf_bootstrap=ok\n")]
    if kind == "slurm":
        responses.append(CommandResult(argv=(), returncode=0, stdout="12345\n"))
    elif kind == "windows_task":
        responses.append(CommandResult(argv=(), returncode=0))
    runner = ScriptedRunner(responses)
    transport = SshTransport("fake", runner)
    cls = {"ssh": SshExecutor, "slurm": SlurmExecutor,
           "windows_task": WindowsTaskExecutor}[kind]
    cls(node, transport).submit(spec)
    upload = runner.inputs[0]
    assert upload
    assert "rcf-pilot.py" in upload and "pilot-args.json" in upload
    files = decoded_upload(upload)
    assert PILOT_SCRIPT in files
    payload = next(json.loads(text) for text in files if '"timeout_seconds"' in text
                   and '"argv"' in text and not text.startswith('"""'))
    assert payload["argv"] == spec.command.argv
    assert payload["timeout_seconds"] == 2
    script = next(text for text in files if text.startswith("#!/usr/bin/env bash")
                  and "rcf-pilot.py" in text)
    assert script.count('python3 "$RUN_DIR/rcf-pilot.py"') == 1
    assert "export SETUP_OK=yes" not in script


def test_slurm_resources_and_mpi_launcher_are_not_rewritten():
    spec = job()
    spec.resources.tasks = 4
    script = build_sbatch_script(job=spec, run_dir=spec.run_dir, slurm=SlurmSpec())
    assert "#SBATCH --ntasks=4" in script
    assert "rcf-pilot.py" in script
    assert json.loads(pilot_files(spec.command, 2)["pilot-args.json"])["argv"][:3] == [
        "mpirun", "-np", "4"]


def test_regular_job_does_not_gain_watchdog():
    spec = job()
    spec.metadata.clear()
    text = build_sbatch_script(job=spec, run_dir=spec.run_dir, slurm=SlurmSpec())
    assert "rcf-pilot.py" not in text
    assert "export SETUP_OK=yes" in text
    assert "mpirun" in text
