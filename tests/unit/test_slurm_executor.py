"""Slurm submit edge cases: never duplicate a job because of a parsing hiccup."""

from __future__ import annotations

import pytest

from rcf.core.errors import SchedulerError
from rcf.core.models import JobSpec, NodeSpec, PlannedCommand, ResourceRequirements
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.executors.slurm_exec import SlurmExecutor
from rcf.executors.ssh import SshTransport


def make_executor(responses) -> tuple[SlurmExecutor, ScriptedRunner]:
    node = NodeSpec(
        id="hpc",
        kind="slurm",
        ssh_alias="hpc",
        work_root="/home/me/rcf",
        slurm={"default_partition": "gpu"},
    )
    runner = ScriptedRunner(responses)
    return SlurmExecutor(node, SshTransport("hpc", runner)), runner


def job_spec() -> JobSpec:
    return JobSpec(
        run_id="run_1",
        run_dir="/home/me/rcf/runs/run_1",
        command=PlannedCommand(argv=["python", "train.py"], workdir="/home/me/proj"),
        log_path="/home/me/rcf/runs/run_1/run.log",
        resources=ResourceRequirements(gpus=1),
    )


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout)


def test_parsable_job_id_is_parsed():
    executor, runner = make_executor([ok(), ok("1234\n")])
    receipt = executor.submit(job_spec())
    assert receipt.handle.job_id == "1234"
    assert len(runner.calls) == 2


def test_cluster_suffix_on_job_id_is_tolerated():
    executor, _ = make_executor([ok(), ok("1234;testcluster\n")])
    assert executor.submit(job_spec()).handle.job_id == "1234"


def test_empty_output_with_success_is_not_retried():
    """rc=0 means the job exists — a second sbatch would duplicate the work."""

    executor, runner = make_executor([ok(), ok("")])
    with pytest.raises(SchedulerError, match="no job id"):
        executor.submit(job_spec())
    assert len(runner.calls) == 2  # one upload, one sbatch — no retry


def test_unknown_parsable_flag_falls_back_to_textual_output():
    unsupported = CommandResult(
        argv=(),
        returncode=1,
        stderr="sbatch: unrecognized option '--parsable'\nTry 'sbatch --help'",
    )
    executor, runner = make_executor([ok(), unsupported, ok("Submitted batch job 77\n")])
    receipt = executor.submit(job_spec())
    assert receipt.handle.job_id == "77"
    assert len(runner.calls) == 3


def test_real_rejection_is_not_retried():
    rejected = CommandResult(
        argv=(),
        returncode=1,
        stderr="sbatch: error: Batch job submission failed: Invalid partition name specified",
    )
    executor, runner = make_executor([ok(), rejected])
    with pytest.raises(SchedulerError, match="sbatch failed"):
        executor.submit(job_spec())
    assert len(runner.calls) == 2
