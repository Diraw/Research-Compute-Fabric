"""Dispatch is at-most-once; missing acknowledgements never prove cancellation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from conftest import make_home

from rcf.core.config import Config
from rcf.core.errors import Conflict, HostUnreachable, SchedulerError
from rcf.core.ids import now_utc
from rcf.core.models import (
    JobHandle,
    JobStatus,
    PlannedCommand,
    RunPlan,
    RunRecord,
    RunRequest,
    RunState,
    SubmissionReceipt,
)
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.executors.slurm_exec import SlurmExecutor
from rcf.executors.ssh import SshTransport
from rcf.executors.ssh_exec import SshExecutor
from rcf.runs.store import Store
from rcf.service import RcfService


@pytest.fixture
def service(tmp_path):
    home = make_home(tmp_path, nodes='[nodes.test]\nkind = "local"\n')
    svc = RcfService(Config.load(home), actor="test")
    yield svc
    svc.close()


def save_plan(service, *, gpu=False, old=False):
    plan = RunPlan(
        plan_id="plan_safety",
        run_id="run_safety",
        created_at=now_utc() - timedelta(hours=2) if old else now_utc(),
        request=RunRequest(project_id="demo"),
        node_id="test",
        node_kind="local",
        executor="local",
        command=PlannedCommand(argv=["never-execute"], workdir="/review"),
        workdir="/review",
        run_dir=str(service.config.home / "runs" / "run_safety"),
        gpu_leases=["GPU-0"] if gpu else [],
        fingerprint="safety",
    )
    service.store.save_plan(plan)
    return plan


class LiveExecutor:
    def __init__(self):
        self.submissions = 0

    def submit(self, spec):
        self.submissions += 1
        return SubmissionReceipt(
            handle=JobHandle(kind="local", node_id="test", job_id=str(self.submissions)),
            state=RunState.RUNNING,
        )

    def status(self, handle):
        return JobStatus(state=RunState.RUNNING, observed_at=now_utc())


def test_two_service_connections_dispatch_only_once(service, monkeypatch):
    plan = save_plan(service, gpu=True)
    other = RcfService(Config.load(service.config.home), actor="test-other")
    gate = Barrier(2)
    executor = LiveExecutor()
    # Initialize lazy collaborators before exercising cross-connection writes.
    managers = [service.runs, other.runs]
    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: executor)

    def submit(manager):
        gate.wait(timeout=10)
        return manager.submit(plan.plan_id, confirmed=True)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(submit, managers))
        assert executor.submissions == 1
        assert {record.run_id for record in records} == {plan.run_id}
    finally:
        other.close()


@pytest.mark.parametrize("observation_fails", [False, True])
def test_stale_observation_cannot_resurrect_cancelled_run(service, monkeypatch, observation_fails):
    plan = save_plan(service, gpu=True)
    record = RunRecord(
        run_id=plan.run_id, node_id="test", executor="local", state="pending", created_at=now_utc(),
        handle=JobHandle(kind="local", node_id="test", workdir=plan.run_dir),
    )
    service.store.insert_run(record)
    service.store.acquire_leases(run_id=record.run_id, node_id="test", keys=["GPU-0"])
    observing, cancelled = Event(), Event()

    class DelayedObservation(LiveExecutor):
        def status(self, handle):
            observing.set()
            assert cancelled.wait(timeout=10)
            if observation_fails:
                raise HostUnreachable("stale observation failed")
            return JobStatus(state=RunState.RUNNING, observed_at=now_utc())

        def cancel(self, handle):
            return JobStatus(state=RunState.CANCELLED, observed_at=now_utc())

    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: DelayedObservation())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.runs.refresh, record.run_id)
        try:
            assert observing.wait(timeout=10)
            assert service.runs.cancel(record.run_id, confirmed=True).state is RunState.CANCELLED
        finally:
            cancelled.set()
        assert future.result(timeout=10).state is RunState.CANCELLED
    persisted = service.store.get_run(record.run_id)
    assert persisted.state is RunState.CANCELLED
    assert persisted.finished_at is not None
    assert service.store.active_leases("test") == {}


def test_store_rejects_stale_write_from_another_connection(tmp_path):
    path = tmp_path / "cas.db"
    first, second = Store(path), Store(path)
    try:
        initial = RunRecord(run_id="run_cas", node_id="test", executor="local", state="running", created_at=now_utc())
        first.insert_run(initial)
        stale = second.get_run(initial.run_id)
        terminal = first.get_run(initial.run_id)
        terminal.state = RunState.CANCELLED
        terminal.finished_at = now_utc()
        assert first.update_run(terminal)
        assert not second.update_run(stale)
        assert second.get_run(initial.run_id).state is RunState.CANCELLED
    finally:
        first.close()
        second.close()


def test_repeated_live_plan_dispatches_only_once(service, monkeypatch):
    plan = save_plan(service)
    executor = LiveExecutor()
    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: executor)
    first = service.runs.submit(plan.plan_id, confirmed=True)
    second = service.runs.submit(plan.plan_id, confirmed=True)
    assert executor.submissions == 1
    assert second.handle.job_id == first.handle.job_id


def test_existing_run_is_returned_even_after_plan_expiry(service, monkeypatch):
    plan = save_plan(service, old=True)
    service.store.insert_run(
        RunRecord(run_id=plan.run_id, node_id="test", executor="local", state="running", created_at=now_utc())
    )
    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: pytest.fail("must not dispatch"))
    assert service.runs.submit(plan.plan_id, confirmed=True).state is RunState.RUNNING


def test_unknown_submit_is_not_retried_and_keeps_recovery_handle(service, monkeypatch):
    plan = save_plan(service, gpu=True)

    class DroppedReply(LiveExecutor):
        def submit(self, spec):
            self.submissions += 1
            raise HostUnreachable("reply lost; job may have started")

    executor = DroppedReply()
    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: executor)
    with pytest.raises(HostUnreachable):
        service.runs.submit(plan.plan_id, confirmed=True)
    record = service.runs.submit(plan.plan_id, confirmed=True)
    assert executor.submissions == 1
    assert record.state is RunState.UNKNOWN
    assert record.handle.workdir == plan.run_dir
    assert service.store.active_leases("test") == {"GPU-0": plan.run_id}


def test_cancel_without_handle_never_releases_unknown_lease(service):
    plan = save_plan(service, gpu=True)
    service.store.insert_run(
        RunRecord(run_id=plan.run_id, plan_id=plan.plan_id, node_id="test", executor="local",
                  state="unknown", created_at=now_utc())
    )
    service.store.acquire_leases(run_id=plan.run_id, node_id="test", keys=["GPU-0"])
    with pytest.raises(Conflict, match="confirm|recover|unknown"):
        service.runs.cancel(plan.run_id, confirmed=True)
    assert service.store.get_run(plan.run_id).state is RunState.UNKNOWN
    assert service.store.active_leases("test") == {"GPU-0": plan.run_id}


def test_nonterminal_cancel_result_keeps_lease(service, monkeypatch):
    plan = save_plan(service, gpu=True)
    record = RunRecord(
        run_id=plan.run_id, node_id="test", executor="local", state="running", created_at=now_utc(),
        handle=JobHandle(kind="local", node_id="test", workdir=plan.run_dir),
    )
    service.store.insert_run(record)
    service.store.acquire_leases(run_id=plan.run_id, node_id="test", keys=["GPU-0"])

    class NotCancelled(LiveExecutor):
        def cancel(self, handle):
            return JobStatus(state=RunState.PENDING, observed_at=now_utc(), detail="still queued")

    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: NotCancelled())
    result = service.runs.cancel(plan.run_id, confirmed=True)
    assert not result.state.terminal
    assert result.finished_at is None
    assert service.store.active_leases("test") == {"GPU-0": plan.run_id}


def test_claim_and_gpu_reservation_are_atomic_across_connections(tmp_path):
    path = tmp_path / "state.db"
    stores = [Store(path), Store(path)]
    gate = Barrier(2)
    record = RunRecord(run_id="run_claim", node_id="test", executor="local", state="submitting", created_at=now_utc())

    def claim(store):
        gate.wait(timeout=10)
        return store.claim_submission(record, keys=["GPU-0"])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, stores))
        assert sorted(claimed for _, claimed in results) == [False, True]
        assert stores[0].active_leases("test") == {"GPU-0": record.run_id}
    finally:
        for store in stores:
            store.close()


def test_failed_claim_rolls_back_partial_gpu_reservation(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        store.acquire_leases(run_id="other", node_id="test", keys=["GPU-b"])
        record = RunRecord(run_id="run_claim", node_id="test", executor="local", state="submitting", created_at=now_utc())
        with pytest.raises(Conflict):
            store.claim_submission(record, keys=["GPU-a", "GPU-b"])
        assert store.get_run(record.run_id) is None
        assert store.active_leases("test") == {"GPU-b": "other"}
    finally:
        store.close()


def test_sbatch_success_without_id_is_unknown_not_retryable(tmp_path, monkeypatch):
    home = make_home(tmp_path, nodes='[nodes.hpc]\nkind = "slurm"\nssh_alias = "never-connect"\n[nodes.hpc.slurm]\ndefault_partition = "cpu"\n')
    service = RcfService(Config.load(home), actor="test")
    try:
        plan = RunPlan(
            plan_id="plan_slurm", run_id="run_slurm", created_at=now_utc(),
            request=RunRequest(project_id="demo"), node_id="hpc", node_kind="slurm", executor="slurm",
            command=PlannedCommand(argv=["true"], workdir="/review"), workdir="/review", run_dir="/review/run",
        )
        service.store.save_plan(plan)
        ok = CommandResult(argv=(), returncode=0, stdout="")
        runner = ScriptedRunner([ok, ok, ok, ok])
        executor = SlurmExecutor(service.config.node("hpc"), SshTransport("never-connect", runner))
        monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: executor)
        with pytest.raises(SchedulerError, match="no job id"):
            service.runs.submit(plan.plan_id, confirmed=True)
        record = service.runs.submit(plan.plan_id, confirmed=True)
        assert record.state is RunState.UNKNOWN
        assert record.handle.workdir == plan.run_dir
        assert len(runner.calls) == 2
    finally:
        service.close()


def test_slurm_cancel_rejection_is_not_reported_as_cancelled():
    from rcf.core.models import NodeSpec

    node = NodeSpec(id="hpc", kind="slurm", ssh_alias="never-connect", slurm={})
    runner = ScriptedRunner([CommandResult(argv=(), returncode=1, stderr="permission denied")])
    executor = SlurmExecutor(node, SshTransport("never-connect", runner))
    with pytest.raises(SchedulerError):
        executor.cancel(JobHandle(kind="slurm", node_id="hpc", job_id="123", workdir="/review"))


def test_ssh_cancel_without_pid_does_not_claim_success(monkeypatch):
    from rcf.core.models import NodeSpec

    node = NodeSpec(id="ws", kind="ssh", ssh_alias="never-connect")
    runner = ScriptedRunner([
        CommandResult(argv=(), returncode=0, stdout="cancelled=no_pid\nstate=NOT_RUNNING\n"),
        CommandResult(argv=(), returncode=0, stdout="state=STARTING\n"),
    ])
    ticks = iter([0.0, 4.0])
    monkeypatch.setattr("rcf.executors.ssh_exec.time.monotonic", lambda: next(ticks))
    executor = SshExecutor(node, SshTransport("never-connect", runner))
    result = executor.cancel(JobHandle(kind="ssh", node_id="ws", workdir="/review"))
    assert result.state is RunState.UNKNOWN


def test_slurm_status_recovers_persisted_job_id():
    from rcf.core.models import NodeSpec

    node = NodeSpec(id="hpc", kind="slurm", ssh_alias="never-connect", slurm={})
    ok = lambda text="": CommandResult(argv=(), returncode=0, stdout=text)
    runner = ScriptedRunner([ok(), ok("123\n"), ok(), ok("PENDING|0:00|node001|Priority\n")])
    executor = SlurmExecutor(node, SshTransport("never-connect", runner))
    handle = JobHandle(kind="slurm", node_id="hpc", workdir="/review")
    assert executor.status(handle).state is RunState.PENDING
    assert handle.job_id == "123"


def test_unknown_empty_run_directory_stays_unknown(service, monkeypatch):
    plan = save_plan(service, gpu=True)
    service.store.insert_run(RunRecord(
        run_id=plan.run_id, node_id="test", executor="local", state="unknown", created_at=now_utc(),
        handle=JobHandle(kind="local", node_id="test", workdir=plan.run_dir,
                         extra={"submission_uncertain": True}),
    ))
    service.store.acquire_leases(run_id=plan.run_id, node_id="test", keys=["GPU-0"])

    class Starting(LiveExecutor):
        def status(self, handle):
            return JobStatus(state=RunState.PENDING, observed_at=now_utc())

    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a: Starting())
    assert service.runs.refresh(plan.run_id).state is RunState.UNKNOWN
    with pytest.raises(Conflict):
        service.runs.cancel(plan.run_id, confirmed=True)
    assert service.store.active_leases("test") == {"GPU-0": plan.run_id}
