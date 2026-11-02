"""Synthetic research lifecycle: inventory, ownership, protected data and cleanup."""

from __future__ import annotations

import json

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import Conflict, PermissionDenied
from rcf.core.ids import now_utc
from rcf.core.models import JobHandle, RunRecord
from rcf.service import RcfService


@pytest.fixture
def research(tmp_path):
    source, target, data, outputs = [tmp_path / name for name in ("source", "target", "data", "run/outputs")]
    for root in (source, target, data, outputs):
        root.mkdir(parents=True)
    settings = '[security]\nrequire_confirmation = false\n[storage]\nkeep_recent_runs = 0\n'
    home = make_home(tmp_path, settings=settings, nodes='[nodes.laptop]\nkind="local"\n[nodes.workstation]\nkind="local"\n',
                     projects='[projects.demo]\n' +
                     f'paths={{laptop="{toml_path(source)}",workstation="{toml_path(target)}"}}\n' +
                     f'data_paths={{workstation="{toml_path(data)}"}}\n' +
                     'disposable_globs=["*.tmp"]\nstorage_keep_recent_runs=0\n')
    svc = RcfService(Config.load(home), actor="test")
    marker = outputs.parent / "run.json"
    marker.write_text(json.dumps({"run_id": "run_demo"}), encoding="utf-8")
    record = RunRecord(run_id="run_demo", project_id="demo", node_id="workstation", executor="local",
                       state="succeeded", created_at=now_utc(), finished_at=now_utc(), result_dir=str(outputs),
                       workdir=str(target), handle=JobHandle(kind="local", node_id="workstation", workdir=str(outputs.parent)))
    svc.store.insert_run(record)
    yield svc, source, target, data, outputs
    svc.close()


def test_inventory_accounts_unknown_growth_and_budget(research):
    svc, _, target, _, _ = research
    (target / "old.dat").write_bytes(b"123")
    result = svc.scan_storage("demo", "workstation")
    assert result["snapshots"]
    report = svc.report_storage("demo", "workstation")
    assert report["classification"]["unmanaged"]["logical_bytes"] == 3
    (target / "next.dat").write_bytes(b"12345")
    later = svc.scan_storage("demo", "workstation")
    code = next(s for s in later["snapshots"] if s["root_id"] == "code")
    assert code["growth_bytes"] == 5


def test_explicit_adoption_is_not_deletion_and_apply_always_needs_confirmation(research):
    svc, _, target, _, _ = research
    path = target / "legacy.tmp"
    path.write_bytes(b"legacy")
    plan = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=["legacy.tmp"], kind="disposable")
    with pytest.raises(PermissionDenied):
        svc.apply_storage_plan(plan["plan_id"])
    assert path.exists()
    adopted = svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert adopted["registered"] == 1
    assert path.exists()
    cleanup = svc.plan_storage_cleanup("demo", "workstation", kind="disposable", root_id="code")
    assert [e["path"] for e in cleanup["entries"]] == ["legacy.tmp"]
    with pytest.raises(PermissionDenied):
        svc.apply_storage_plan(cleanup["plan_id"])
    removed = svc.apply_storage_plan(cleanup["plan_id"], confirm=True)
    assert removed["deleted"] == ["legacy.tmp"]
    assert not path.exists()
    assert svc.apply_storage_plan(cleanup["plan_id"], confirm=True) == removed


def test_stale_deployed_code_prunes_only_unchanged_owned_files(research):
    svc, source, target, _, _ = research
    (source / "removed.py").write_text("x=1\n")
    (source / "remote_edit.py").write_text("x=2\n")
    (target / "manual.py").write_text("manual\n")
    svc.push_project("demo", "workstation", source_node_id="laptop", confirm=True)
    (source / "removed.py").unlink()
    (source / "remote_edit.py").unlink()
    (target / "remote_edit.py").write_text("x=3\n")
    plan = svc.plan_storage_cleanup("demo", "workstation", kind="stale-code", source_node_id="laptop")
    assert [e["path"] for e in plan["entries"]] == ["removed.py"]
    assert any(e["path"] == "remote_edit.py" for e in plan["blocked"])
    svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert not (target / "removed.py").exists()
    assert (target / "manual.py").exists()
    assert (target / "remote_edit.py").read_text() == "x=3\n"


def test_legacy_code_can_be_adopted_only_when_it_matches_source(research):
    svc, source, target, _, _ = research
    (source / "legacy.py").write_text("legacy = True\n")
    (target / "legacy.py").write_text("legacy = True\n")
    plan = svc.plan_storage_adoption(
        "demo", "workstation", root_id="code", paths=["legacy.py"], kind="code",
        source_node_id="laptop",
    )
    svc.apply_storage_plan(plan["plan_id"], confirm=True)
    (source / "legacy.py").unlink()
    cleanup = svc.plan_storage_cleanup(
        "demo", "workstation", kind="stale-code", source_node_id="laptop",
    )
    assert [e["path"] for e in cleanup["entries"]] == ["legacy.py"]
    svc.apply_storage_plan(cleanup["plan_id"], confirm=True)
    assert not (target / "legacy.py").exists()


def test_legacy_code_adoption_rejects_a_remote_only_edit(research):
    svc, source, target, _, _ = research
    (source / "edited.py").write_text("version = 1\n")
    (target / "edited.py").write_text("version = 2\n")
    with pytest.raises(Conflict, match="do not match"):
        svc.plan_storage_adoption(
            "demo", "workstation", root_id="code", paths=["edited.py"], kind="code",
            source_node_id="laptop",
        )


def test_input_data_root_stays_protected_even_when_marked_disposable(research):
    svc, _, _, data, _ = research
    (data / "original.bin").write_bytes(b"irreplaceable")
    adoption = svc.plan_storage_adoption("demo", "workstation", root_id="data", paths=["original.bin"], kind="disposable")
    svc.apply_storage_plan(adoption["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="data", kind="disposable")
    assert not plan["entries"]
    assert "protected" in " ".join(plan["blocked"][0]["reasons"])
    svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert (data / "original.bin").exists()


def test_run_plan_can_register_outputs_without_a_prior_project_scan(research):
    svc, _, _, _, outputs = research
    (outputs / "scratch.tmp").write_bytes(b"temp")
    plan = svc.plan_storage_cleanup("demo", "workstation", run_id="run_demo", kind="disposable")
    assert [e["path"] for e in plan["entries"]] == ["scratch.tmp"]


def test_registered_input_is_protected_even_outside_the_data_root(research):
    svc, _, target, _, _ = research
    path = target / "declared.tmp"
    path.write_bytes(b"input")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="input")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    assert not plan["entries"]
    svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert path.exists()


def test_pin_and_post_plan_changes_block_cleanup(research):
    svc, _, _, _, outputs = research
    (outputs / "scratch.tmp").write_bytes(b"temp")
    (outputs / "paper.csv").write_bytes(b"important")
    svc.scan_storage("demo", "workstation")
    plan = svc.plan_storage_cleanup("demo", "workstation", run_id="run_demo", kind="disposable")
    assert [e["path"] for e in plan["entries"]] == ["scratch.tmp"]
    svc.pin_run("run_demo", reason="paper result", confirm=True)
    with pytest.raises(Conflict):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    pinned = svc.plan_storage_cleanup("demo", "workstation", run_id="run_demo", kind="disposable")
    assert not pinned["entries"]
    assert (outputs / "paper.csv").exists()


def test_modified_file_blocks_plan_without_touching_it(research):
    svc, _, target, _, _ = research
    path = target / "scratch.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    path.write_bytes(b"xyz")
    with pytest.raises(Conflict):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert path.read_bytes() == b"xyz"


def test_interrupted_cleanup_resumes_exact_plan_and_adopts_guard(research):
    svc, _, target, _, _ = research
    path = target / "scratch.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    # Simulate a controller death after dispatch: the plan is unknown and its
    # guard remains owned by this exact plan.
    svc.storage.ledger.update_plan(plan["plan_id"], "unknown")
    svc.storage.ledger.acquire_guard("workstation", [str(target)], operation="storage.apply", owner_id=plan["plan_id"])
    with pytest.raises(Conflict):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    result = svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    assert result["status"] == "completed"
    assert not path.exists()
    assert svc.storage.ledger.guards("workstation") == []


def test_resume_can_reclaim_a_claim_that_crashed_before_its_guard(research):
    svc, _, target, _, _ = research
    path = target / "early-crash.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    # Crash after the atomic claim but before acquiring any root guard: no
    # deletion could have started, so an explicit resume may reclaim it.
    svc.storage.ledger.update_plan(plan["plan_id"], "applying")
    result = svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    assert result["status"] == "completed"
    assert not path.exists()


def test_resume_does_not_steal_a_plan_that_still_holds_its_guard(research):
    svc, _, target, _, _ = research
    path = target / "held.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    svc.storage.ledger.update_plan(plan["plan_id"], "applying")
    svc.storage.ledger.acquire_guard("workstation", [str(target)], operation="storage.apply", owner_id=plan["plan_id"])
    with pytest.raises(Conflict):
        svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    assert path.exists()


def test_resume_accepts_file_already_removed_by_first_attempt(research):
    svc, _, target, _, _ = research
    path = target / "gone.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    path.unlink()
    svc.storage.ledger.update_plan(plan["plan_id"], "unknown")
    result = svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    assert result["status"] == "completed"
    assert "gone.tmp" in result["missing"]


def test_dead_apply_owner_with_guard_is_recoverable_without_ttl_guessing(research, monkeypatch):
    from rcf.core.errors import ExecutorError

    svc, _, target, _, _ = research
    path = target / "lost-reply.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    real_delete = svc.storage.fs.delete

    def lost_reply(*args, **kwargs):
        real_delete(*args, **kwargs)
        raise ExecutorError("synthetic reply lost after deletion")

    monkeypatch.setattr(svc.storage.fs, "delete", lost_reply)
    with pytest.raises(ExecutorError):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    detail = svc.get_storage_plan(plan["plan_id"])
    assert detail["plan"]["status"] == "unknown"
    assert detail["plan"]["execution"]["stage"] == "outcome_unknown"
    assert detail["guards"]
    assert not path.exists()
    monkeypatch.setattr(svc.storage.fs, "delete", real_delete)
    recovered = svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    assert recovered["status"] == "completed"
    assert svc.get_storage_plan(plan["plan_id"])["guards"] == []


@pytest.mark.parametrize("fault", ["interrupt", "unknown-save-fails"])
def test_dispatch_failure_never_releases_guard_even_if_persistence_fails(research, monkeypatch, fault):
    from rcf.core.errors import ExecutorError

    svc, _, target, _, _ = research
    path = target / "uncertain.tmp"
    path.write_bytes(b"abc")
    adoption = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adoption["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    error = KeyboardInterrupt("synthetic interrupt") if fault == "interrupt" else ExecutorError("reply lost")

    def broken_delete(*args, **kwargs):
        raise error

    monkeypatch.setattr(svc.storage.fs, "delete", broken_delete)
    update = svc.storage.ledger.update_plan

    def failed_unknown_write(plan_id, status, *args, **kwargs):
        if status == "unknown":
            raise RuntimeError("database unavailable")
        return update(plan_id, status, *args, **kwargs)

    if fault == "unknown-save-fails":
        monkeypatch.setattr(svc.storage.ledger, "update_plan", failed_unknown_write)
    with pytest.raises(type(error)):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert svc.get_storage_plan(plan["plan_id"])["guards"]
    assert path.exists()


def test_changed_reference_unknown_can_reconcile_without_deleting(research, monkeypatch):
    from rcf.core.errors import ExecutorError

    svc, _, target, _, _ = research
    path = target / "reconcile.tmp"
    path.write_bytes(b"abc")
    adoption = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adoption["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    real_delete = svc.storage.fs.delete

    def dropped_reply(*args, **kwargs):
        raise ExecutorError("synthetic dispatch lost")

    monkeypatch.setattr(svc.storage.fs, "delete", dropped_reply)
    with pytest.raises(ExecutorError):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    monkeypatch.setattr(svc.storage.fs, "delete", real_delete)
    svc.storage.ledger.set_pin("another-project", "another-run", True, "unrelated revision")
    with pytest.raises(Conflict, match="references"):
        svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    with pytest.raises(PermissionDenied):
        svc.reconcile_storage_plan(plan["plan_id"])
    result = svc.reconcile_storage_plan(plan["plan_id"], confirm=True)
    assert result["status"] == "superseded"
    assert result["no-delete"] is True
    assert path.exists()
    assert not svc.get_storage_plan(plan["plan_id"])["guards"]
    new_plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    svc.apply_storage_plan(new_plan["plan_id"], confirm=True)
    assert not path.exists()
    assert svc.apply_storage_plan(plan["plan_id"], confirm=True)["no-delete"] is True


def test_running_apply_owner_cannot_be_resumed_in_second_thread(research):
    from rcf.storage.execution import execution_lock

    svc, _, target, _, _ = research
    path = target / "live-owner.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    with execution_lock(svc.config.home, plan["plan_id"]):
        with pytest.raises(Conflict, match="live process"):
            svc.apply_storage_plan(plan["plan_id"], confirm=True, resume=True)
    assert path.exists()


def test_two_processes_cannot_apply_same_plan(research):
    svc, _, target, _, _ = research
    path = target / "race.tmp"
    path.write_bytes(b"abc")
    adopt = svc.plan_storage_adoption("demo", "workstation", root_id="code", paths=[path.name], kind="disposable")
    svc.apply_storage_plan(adopt["plan_id"], confirm=True)
    plan = svc.plan_storage_cleanup("demo", "workstation", root_id="code", kind="disposable")
    claimed = svc.storage.ledger.claim_plan(plan["plan_id"])
    assert claimed["_claimed"] is True
    with pytest.raises(Conflict):
        svc.storage.ledger.claim_plan(plan["plan_id"])


def test_active_run_and_cleanup_guard_block_each_other(research):
    svc, _, target, _, _ = research
    token = svc.storage.ledger.acquire_guard("workstation", [str(target)], operation="cleanup", owner_id="test")
    try:
        with pytest.raises(Conflict):
            svc.push_project("demo", "workstation", source_node_id="laptop", confirm=True)
    finally:
        svc.storage.ledger.release_guard(token)


def test_sole_copy_result_not_candidate_but_verified_copy_is(research, tmp_path):
    svc, _, _, _, outputs = research
    (outputs / "paper.csv").write_bytes(b"important")
    svc.scan_storage("demo", "workstation")
    before = svc.plan_storage_cleanup("demo", "workstation", run_id="run_demo", kind="replica")
    assert not before["entries"]
    copied = svc.fetch_results("run_demo", dest=str(tmp_path / "copy"), confirm=True)
    assert copied["copy_receipt"]["verified"]
    plan = svc.plan_storage_cleanup("demo", "workstation", run_id="run_demo", kind="replica")
    assert [e["path"] for e in plan["entries"]] == ["paper.csv"]
    (tmp_path / "copy/paper.csv").write_bytes(b"different")
    with pytest.raises(Conflict):
        svc.apply_storage_plan(plan["plan_id"], confirm=True)
    assert (outputs / "paper.csv").exists()
