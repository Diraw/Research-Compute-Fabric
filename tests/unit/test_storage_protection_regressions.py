"""Counterexamples for physical aliases and scientific input/result protection."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import Conflict
from rcf.core.ids import now_utc
from rcf.core.models import JobHandle, NodeSpec, RunRecord, SshOptions
from rcf.runs.store import Store
from rcf.service import RcfService
from rcf.storage.ledger import Ledger
from rcf.storage.roots import normalized, reference_fingerprint


def service_for(tmp_path, *, inputs=(), keep=0):
    code = tmp_path / "checkout"
    code.mkdir()
    home = make_home(
        tmp_path, settings=f"[storage]\nkeep_recent_runs={keep}\n",
        nodes='[nodes.local]\nkind="local"\n[nodes.alias]\nkind="local"\n',
        projects='[projects.demo]\npaths={local="' + toml_path(code)
        + '",alias="' + toml_path(code) + '"}\ndisposable_globs=["*.tmp"]\n'
        + "inputs=" + json.dumps(list(inputs)) + "\n",
    )
    return RcfService(Config.load(home), actor="test"), code


def add_run(service, run_id, code, output, workdir, *, age=0):
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    service.store.insert_run(RunRecord(
        run_id=run_id, project_id="demo", node_id="local", executor="local",
        state="succeeded", created_at=now_utc() - timedelta(days=age),
        finished_at=now_utc() - timedelta(days=age), workdir=str(code), result_dir=str(output),
        handle=JobHandle(kind="local", node_id="local", workdir=str(workdir)),
    ))


def test_two_local_replica_cleanups_cannot_delete_both_copies(tmp_path, monkeypatch):
    service, code = service_for(tmp_path)
    with service:
        outputs = [tmp_path / name / "outputs" for name in ("run_A", "run_B")]
        for name, output in zip(("run_A", "run_B"), outputs, strict=True):
            output.mkdir(parents=True)
            add_run(service, name, code, output, output.parent)
        (outputs[0] / "result.dat").write_bytes(b"only result")
        service.fetch_results("run_A", dest=str(outputs[1]), confirm=True)
        service.fetch_results("run_B", dest=str(outputs[0]), force=True, confirm=True)
        service.scan_storage("demo", "local")
        plans = [service.plan_storage_cleanup("demo", "local", run_id=name, kind="replica")
                 for name in ("run_A", "run_B")]
        assert all(len(p["entries"]) == 1 for p in plans)
        retained_checked, second_finished = Event(), Event()
        original = service.storage.fs.inspect

        def inspect(node, root, paths, **kwargs):
            result = original(node, root, paths, **kwargs)
            if node.id == "controller" and root == str(outputs[1]) and kwargs.get("hashes"):
                retained_checked.set()
                assert second_finished.wait(20)
            return result

        monkeypatch.setattr(service.storage.fs, "inspect", inspect)

        def apply(plan):
            try:
                return service.apply_storage_plan(plan["plan_id"], confirm=True)
            except Conflict:
                return {"status": "conflict"}

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(apply, plans[0])
            assert retained_checked.wait(20)
            try:
                second = pool.submit(apply, plans[1]).result(timeout=20)
            finally:
                second_finished.set()
            first_report = first.result(timeout=20)
        assert second["status"] == "conflict"
        assert first_report["status"] == "completed"
        assert (outputs[1] / "result.dat").read_bytes() == b"only result"


@pytest.mark.parametrize("first,second", [("local", "controller"), ("controller", "alias"), ("local", "alias")])
def test_guard_physical_identity_cannot_be_bypassed_by_local_node_name(tmp_path, first, second):
    service, code = service_for(tmp_path)
    with service:
        ledger = service.storage.ledger
        token = ledger.acquire_guard(first, [str(code)], operation="storage.apply", owner_id="cleanup")
        # A fresh adapter (as used by run.submit) must share the physical identity.
        alternate = Ledger(service.store)
        with pytest.raises(Conflict):
            alternate.acquire_guard(second, [str(code / "data")], operation="fetch", owner_id="run")
        other_store = Store(service.store.path)
        try:
            with pytest.raises(Conflict):
                Ledger(other_store).acquire_guard(second, [str(code)], operation="fetch", owner_id="other")
        finally:
            other_store.close()
        ledger.release_guard(token)


@pytest.mark.parametrize("legacy_node", ["removed-local-alias", "alias"])
def test_legacy_unknown_guard_is_not_silently_bypassed(tmp_path, legacy_node):
    service, code = service_for(tmp_path)
    with service:
        ledger = service.storage.ledger
        payload = {"guard_id": "legacy", "node_id": legacy_node, "roots": [str(code)],
                   "operation": "storage.apply", "owner_id": "old-plan", "released_at": None}
        with service.store._lock:
            service.store._conn.execute(
                "INSERT INTO storage_guards(guard_id,node_id,payload) VALUES (?,?,?)",
                ("legacy", legacy_node, json.dumps(payload)),
            )
            # A configured alias may have changed from local to remote since
            # this pre-physical-identity guard was written. Current config is
            # not evidence of that old mutation's target.
            service.store._conn.execute(
                "UPDATE storage_guard_nodes SET physical_id='node:alias' WHERE node_id='alias'",
            )
            service.store._conn.commit()
        with pytest.raises(Conflict):
            ledger.acquire_guard("local", [str(code)], operation="fetch", owner_id="new-run")
        assert ledger.guards()[0]["guard_id"] == "legacy"


@pytest.mark.parametrize("protection", ["pin", "recent"])
def test_shared_root_retains_protected_real_owner_and_referenced_output(tmp_path, protection):
    service, code = service_for(tmp_path, keep=1 if protection == "recent" else 0)
    with service:
        output = tmp_path / "shared"
        output.mkdir()
        path = output / "scientific.tmp"
        path.write_bytes(b"irreplaceable")
        add_run(service, "run_A", code, output, tmp_path / "work_A")
        service.scan_storage("demo", "local")
        if protection == "pin":
            service.pin_run("run_A", reason="retain scientific result", confirm=True)
        add_run(service, "run_B", code, output, tmp_path / "work_B", age=1)
        plan = service.plan_storage_cleanup("demo", "local", run_id="run_B", kind="disposable")
        assert plan["entries"] == []
        assert any(e["path"] == path.name for e in plan["blocked"])
        # Even a later ownership reclassification cannot erase A's path reference.
        service.storage.ledger.record_owned("demo", "local", normalized(str(output)),
                                            service.storage.fs.inspect(service.config.node("local"), str(output),
                                                                       [path.name], hashes=True)["entries"],
                                            kind="disposable", owner_id="run_B")
        second = service.plan_storage_cleanup("demo", "local", run_id="run_B", kind="disposable")
        assert second["entries"] == []
        assert path.read_bytes() == b"irreplaceable"


@pytest.mark.parametrize("pattern,relative", [("training.tmp", "training.tmp"),
                                               ("inputs/**/*.dat", "inputs/nested/train.dat"),
                                               ("inputs/**/*.dat", "inputs/train.dat"),
                                               ("inputs/*.tmp", "inputs/train.tmp")])
def test_checkout_declared_inputs_protected_without_any_run(tmp_path, pattern, relative):
    service, code = service_for(tmp_path, inputs=[pattern])
    with service:
        path = code / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"only training data")
        adopt = service.plan_storage_adoption("demo", "local", root_id="code", paths=[relative], kind="disposable")
        service.apply_storage_plan(adopt["plan_id"], confirm=True)
        assert service.store.list_runs() == []
        plan = service.plan_storage_cleanup("demo", "local", root_id="code", kind="disposable")
        assert plan["entries"] == []
        assert any(e["path"] == relative and any("declared input" in r for r in e["reasons"])
                   for e in plan["blocked"])
        assert path.exists()


def test_apply_independently_rechecks_declared_input_not_only_plan_filter(tmp_path):
    service, code = service_for(tmp_path)
    with service:
        path = code / "training.tmp"
        path.write_bytes(b"only training data")
        adopt = service.plan_storage_adoption("demo", "local", root_id="code", paths=[path.name], kind="disposable")
        service.apply_storage_plan(adopt["plan_id"], confirm=True)
        plan = service.plan_storage_cleanup("demo", "local", root_id="code", kind="disposable")
        assert len(plan["entries"]) == 1
        service.config.project("demo").inputs = [path.name]
        # Model an old/external plan already carrying the current reference digest:
        # apply's actual path protection must still reject, not merely the digest.
        plan["reference_fingerprint"] = reference_fingerprint(service.config, service.store, service.storage.ledger)
        plan["plan_id"] += "_external"
        service.storage.ledger.save_plan(plan)
        with pytest.raises(Conflict, match="protected"):
            service.apply_storage_plan(plan["plan_id"], confirm=True)
        assert path.read_bytes() == b"only training data"


@pytest.mark.parametrize("phase", ["plan", "apply"])
def test_actual_pinned_owner_protected_even_outside_its_recorded_output_root(tmp_path, phase):
    service, code = service_for(tmp_path)
    with service:
        output = tmp_path / "result_A"
        output.mkdir()
        add_run(service, "run_A", code, output, tmp_path / "work_A")
        path = code / "owned.tmp"
        path.write_bytes(b"owned by A")
        adopt = service.plan_storage_adoption("demo", "local", root_id="code", paths=[path.name],
                                             kind="disposable", run_id="run_A")
        service.apply_storage_plan(adopt["plan_id"], confirm=True)
        if phase == "plan":
            service.pin_run("run_A", reason="retain", confirm=True)
        plan = service.plan_storage_cleanup("demo", "local", root_id="code", kind="disposable")
        if phase == "plan":
            assert plan["entries"] == []
            assert any("pinned" in reason for block in plan["blocked"] for reason in block["reasons"])
        else:
            assert len(plan["entries"]) == 1
            service.pin_run("run_A", reason="retain", confirm=True)
            plan["reference_fingerprint"] = reference_fingerprint(service.config, service.store, service.storage.ledger)
            plan["plan_id"] += "_external"
            service.storage.ledger.save_plan(plan)
            with pytest.raises(Conflict, match="protected"):
                service.apply_storage_plan(plan["plan_id"], confirm=True)
        assert path.read_bytes() == b"owned by A"


@pytest.mark.parametrize("kind", ["ssh", "slurm"])
def test_remote_same_endpoint_different_node_ids_share_guard(tmp_path, kind):
    store = Store(tmp_path / "remote.db")
    try:
        first = NodeSpec(id="remote-a", kind="ssh", ssh_alias="fake-endpoint")
        second = NodeSpec(id="remote-b", kind=kind, ssh_alias="fake-endpoint",
                          slurm={} if kind == "slurm" else None,
                          name="other display name", work_root="/different-work-root")
        ledger = Ledger(store, nodes={first.id: first, second.id: second})
        ledger.acquire_guard(first.id, ["/shared/results"], operation="storage.apply", owner_id="cleanup")
        with pytest.raises(Conflict):
            Ledger(store).acquire_guard(second.id, ["/shared/results/file"], operation="fetch", owner_id="fetch")
        assert ledger.physical_node(first.id) == ledger.physical_node(second.id)
    finally:
        store.close()


@pytest.mark.parametrize("change", ["alias", "connection", "removed"])
def test_remote_endpoint_config_drift_does_not_bypass_old_guard(tmp_path, change):
    store = Store(tmp_path / "drift.db")
    try:
        original = NodeSpec(id="remote-a", kind="ssh", ssh_alias="old-endpoint")
        ledger = Ledger(store, nodes={original.id: original})
        token = ledger.acquire_guard(original.id, ["/shared"], operation="storage.apply", owner_id="old-plan")
        changed = original.model_copy(update={"ssh_alias": "new-endpoint"} if change in {"alias", "removed"} else {
            "ssh": SshOptions(extra_options=["Port=2222"])})
        alias = changed.model_copy(update={"id": "remote-b"})
        fresh = Ledger(store, nodes={alias.id: alias} if change == "removed" else {changed.id: changed, alias.id: alias})
        with pytest.raises(Conflict):
            fresh.acquire_guard(alias.id, ["/shared/results"], operation="fetch", owner_id="new-run")
        assert fresh.guards()[0]["guard_id"] == token
    finally:
        store.close()


def test_legacy_remote_node_id_guard_not_reinterpreted_using_current_endpoint(tmp_path):
    store = Store(tmp_path / "legacy-remote.db")
    try:
        old = {"guard_id": "legacy-remote", "node_id": "remote-a", "physical_id": "node:remote-a",
               "roots": ["/shared"], "operation": "storage.apply", "owner_id": "old-plan", "released_at": None}
        ledger = Ledger(store)
        with store._lock:
            store._conn.execute("INSERT INTO storage_guards(guard_id,node_id,payload) VALUES (?,?,?)",
                                (old["guard_id"], old["node_id"], json.dumps(old)))
            store._conn.commit()
        a = NodeSpec(id="remote-a", kind="ssh", ssh_alias="new-endpoint")
        b = a.model_copy(update={"id": "remote-b"})
        ledger = Ledger(store, nodes={a.id: a, b.id: b})
        with pytest.raises(Conflict):
            ledger.acquire_guard(b.id, ["/shared/file"], operation="fetch", owner_id="new-run")
        assert ledger.guards()[0] == old
    finally:
        store.close()


@pytest.mark.parametrize("change", ["alias", "connection"])
def test_remote_distinct_connection_identities_keep_separate_guards(tmp_path, change):
    store = Store(tmp_path / "distinct.db")
    try:
        a = NodeSpec(id="remote-a", kind="ssh", ssh_alias="endpoint-a")
        b = a.model_copy(update={"id": "remote-b", **({"ssh_alias": "endpoint-b"} if change == "alias" else {
            "ssh": SshOptions(extra_options=["Port=2222"])})})
        ledger = Ledger(store, nodes={a.id: a, b.id: b})
        ledger.acquire_guard(a.id, ["/shared"], operation="storage.apply", owner_id="a")
        ledger.acquire_guard(b.id, ["/shared"], operation="fetch", owner_id="b")
        assert ledger.physical_node(a.id) != ledger.physical_node(b.id)
        assert len(ledger.guards()) == 2
    finally:
        store.close()


def test_legacy_local_alias_guard_resumes_exact_token_without_releasing_exclusion(tmp_path):
    service, code = service_for(tmp_path)
    with service:
        ledger = service.storage.ledger
        payload = {"guard_id": "legacy", "node_id": "alias", "roots": [str(code)],
                   "operation": "storage.apply", "owner_id": "old-plan", "released_at": None}
        with service.store._lock:
            service.store._conn.execute(
                "INSERT INTO storage_guards(guard_id,node_id,payload) VALUES (?,?,?)",
                ("legacy", "alias", json.dumps(payload)),
            )
            service.store._conn.commit()
        assert ledger.acquire_guards("controller", [str(code)], operation="storage.apply",
                                     owner_id="old-plan", resume_owner="old-plan") == ["legacy"]
        with pytest.raises(Conflict):
            ledger.acquire_guard("local", [str(code)], operation="fetch", owner_id="new-run")
        assert ledger.guards()[0] == payload
