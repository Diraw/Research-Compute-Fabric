"""Slurm submit edge cases: never duplicate a job because of a parsing hiccup."""

from __future__ import annotations

import pytest

from rcf.core.errors import SchedulerError
from rcf.core.models import JobHandle, JobSpec, NodeSpec, PlannedCommand, ResourceRequirements, RunState
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


def _handle(job_id: str) -> JobHandle:
    return JobHandle(kind="slurm", node_id="hpc", job_id=job_id, workdir="/home/me/rcf/runs/run_1")


def test_cancelled_job_reports_the_batch_signal_not_zero(monkeypatch):
    """A cancelled allocation says ``0:0``; only the batch step has the signal."""

    executor, _ = make_executor([])
    monkeypatch.setattr(executor, "_read_file", lambda path: None)
    sacct = (
        "267660|CANCELLED by 2038|0:0|00:00:14\n"
        "267660.batch|CANCELLED|0:15|00:00:15\n"
    )
    monkeypatch.setattr(executor.transport, "run", lambda *args, **kwargs: ok(sacct))

    status = executor.status(_handle("267660"))

    assert status.state is RunState.CANCELLED
    assert status.exit_code == 143, "exit 0 would be indistinguishable from a clean finish"


def test_cancel_before_start_leaves_the_exit_code_unknown(monkeypatch):
    """A job killed while pending never ran, so it has no exit code at all."""

    executor, _ = make_executor([])
    monkeypatch.setattr(executor, "_read_file", lambda path: None)
    sacct = "267662|CANCELLED by 2038|0:0|00:00:00\n"
    monkeypatch.setattr(executor.transport, "run", lambda *args, **kwargs: ok(sacct))

    status = executor.status(_handle("267662"))

    assert status.state is RunState.CANCELLED
    assert status.exit_code is None
