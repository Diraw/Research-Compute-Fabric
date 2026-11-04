"""SQLite store: persistence, idempotency, leases and audit."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from rcf.core.errors import Conflict
from rcf.core.models import RunEvent, RunRecord, RunState
from rcf.runs.store import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "state" / "test.db")
    yield db
    db.close()


def make_run(run_id="run_1", **kwargs) -> RunRecord:
    payload = {
        "run_id": run_id,
        "node_id": "laptop",
        "executor": "local",
        "state": RunState.SUBMITTING,
        "created_at": datetime.now(UTC),
        **kwargs,
    }
    return RunRecord(**payload)


def test_run_roundtrip(store):
    record = make_run(params={"seed": 1})
    store.insert_run(record)
    loaded = store.get_run("run_1")
    assert loaded is not None
    assert loaded.params == {"seed": 1}
    assert loaded.state is RunState.SUBMITTING


def test_run_update_persists_state_and_exit_code(store):
    store.insert_run(make_run())
    record = store.get_run("run_1")
    record.state = RunState.SUCCEEDED
    record.exit_code = 0
    record.finished_at = datetime.now(UTC)
    store.update_run(record)

    loaded = store.get_run("run_1")
    assert loaded.state is RunState.SUCCEEDED
    assert loaded.exit_code == 0
    assert loaded.finished_at is not None


def test_idempotency_key_is_unique(store):
    store.insert_run(make_run("run_1", idempotency_key="k1"))
    with pytest.raises(Conflict):
        store.insert_run(make_run("run_2", idempotency_key="k1"))
    assert store.find_by_idempotency("k1").run_id == "run_1"


def test_list_runs_filters_by_state(store):
    store.insert_run(make_run("run_1"))
    done = make_run("run_2")
    done.state = RunState.SUCCEEDED
    store.insert_run(done)

    assert {r.run_id for r in store.list_runs(state=RunState.SUCCEEDED)} == {"run_2"}
    assert len(store.list_runs()) == 2


def test_active_runs_excludes_terminal(store):
    store.insert_run(make_run("run_1"))
    done = make_run("run_2")
    done.state = RunState.FAILED
    store.insert_run(done)
    assert [r.run_id for r in store.active_runs()] == ["run_1"]


def test_leases_are_exclusive_and_releasable(store):
    store.acquire_leases(run_id="run_1", node_id="ws", keys=["GPU-a", "GPU-b"])
    assert store.active_leases("ws") == {"GPU-a": "run_1", "GPU-b": "run_1"}

    with pytest.raises(Conflict) as excinfo:
        store.acquire_leases(run_id="run_2", node_id="ws", keys=["GPU-b"])
    assert excinfo.value.detail["held_by"]["GPU-b"] == "run_1"

    store.release_leases("run_1")
    assert store.active_leases("ws") == {}
    store.acquire_leases(run_id="run_2", node_id="ws", keys=["GPU-b"])


def test_concurrent_lease_acquisition_has_exactly_one_winner(tmp_path):
    """The partial unique index, not the Python lock, decides the winner."""

    path = tmp_path / "leases.db"
    first, second = Store(path), Store(path)
    gate = Barrier(2)

    def take(store: Store, run_id: str) -> str | None:
        gate.wait(timeout=10)
        try:
            store.acquire_leases(run_id=run_id, node_id="ws", keys=["GPU-0"])
            return run_id
        except Conflict:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(take, [first, second], ["run_1", "run_2"]))
        assert results.count(None) == 1
        assert len(first.active_leases("ws")) == 1
    finally:
        first.close()
        second.close()


def test_events_keep_order(store):
    for index in range(3):
        store.add_event(
            RunEvent(run_id="run_1", at=datetime.now(UTC), kind="step", message=str(index))
        )
    events = store.list_events("run_1")
    assert [e.message for e in events] == ["0", "1", "2"]


def test_audit_trail_is_recorded(store):
    store.record_audit(
        actor="cli", operation="run.submit", tier="execute", decision="allowed", run_id="run_1"
    )
    audits = store.list_audits()
    assert audits[0]["operation"] == "run.submit"
    assert audits[0]["decision"] == "allowed"


def test_plan_roundtrip(store):
    from rcf.core.models import PlannedCommand, RunPlan, RunRequest

    plan = RunPlan(
        plan_id="plan_1",
        run_id="run_1",
        created_at=datetime.now(UTC),
        request=RunRequest(project_id="p"),
        node_id="ws",
        node_kind="ssh",
        executor="ssh",
        command=PlannedCommand(argv=["true"], workdir="/w"),
        workdir="/w",
    )
    store.save_plan(plan)
    loaded = store.get_plan("plan_1")
    assert loaded is not None
    assert loaded.command.argv == ["true"]


def test_environment_verification_is_recorded(store):
    store.record_verification(env_id="e", node_id="n", fingerprint="fp", detail={"ok": True})
    assert store.get_verification(env_id="e", node_id="n", fingerprint="fp") is not None
    assert store.get_verification(env_id="e", node_id="n", fingerprint="other") is None
