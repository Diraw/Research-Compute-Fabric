"""Storage ledger tests use only isolated temporary SQLite databases."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from rcf.core.errors import Conflict, NotFound, SpecError
from rcf.core.ids import iso
from rcf.runs.store import Store
from rcf.storage.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path / "ledger.db")
    value = Ledger(store)
    yield value
    store.close()


def snapshot(**overrides):
    return {"project_id": "project", "node_id": "node", "root_id": "output",
            "root": "/work/output", "entries": [{"path": "a.txt", "sha256": "aaa",
                                                  "stat": {"size": 3, "mtime_ns": 123}}],
            **overrides}


def test_old_database_migration_preserves_run_schema_and_rows(tmp_path):
    path = tmp_path / "old.db"
    # This predates Ledger; Store's existing run tables must not be rebuilt.
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, state TEXT, idempotency_key TEXT);"
        "INSERT INTO runs VALUES ('old_run', 'unknown', 'old_key');"
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('version', '1');"
    )
    conn.close()
    store = Store(path)
    try:
        before = [tuple(row) for row in store._conn.execute("PRAGMA table_info(runs)")]
        tables_before = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        value = Ledger(store)
        Ledger(store)
        after = [tuple(row) for row in store._conn.execute("PRAGMA table_info(runs)")]
        assert before == after
        assert tuple(store._conn.execute("SELECT run_id, state FROM runs").fetchone()) == ("old_run", "unknown")
        assert store._conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "1"
        tables_after = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert all(name.startswith("storage_") for name in tables_after - tables_before)
        assert value.revision() == 0
        value.set_pin("project", "old_run", True, "retain")
    finally:
        store.close()
    reopened = Store(path)
    try:
        value = Ledger(reopened)
        assert value.revision() == 1
        assert value.pins()[0]["run_id"] == "old_run"
    finally:
        reopened.close()


def test_snapshots_preserve_comparability_and_scope(ledger):
    instant = datetime(2026, 10, 31, 1, 2, tzinfo=UTC)
    original = snapshot(scan_id="scan_old", observed_at=instant,
                        comparability={"root_realpath": "/physical/output", "excludes": ["cache/**"]})
    ledger.save_snapshot(original)
    generated = ledger.save_snapshot(snapshot(entries=[], observed_at=instant))
    ledger.save_snapshot(snapshot(project_id="other", scan_id="other_project"))
    ledger.save_snapshot(snapshot(node_id="other", scan_id="other_node"))
    ledger.save_snapshot(snapshot(root_id="input", scan_id="other_root"))
    assert generated.startswith("scan_")
    assert ledger.latest_snapshot("project", "node", "output")["scan_id"] == generated
    assert ledger.latest_snapshot("absent", "node", "output") is None
    old = next(item for item in ledger.snapshots("project", "node") if item["scan_id"] == "scan_old")
    assert old["comparability"] == original["comparability"]
    assert old["entries"] == original["entries"]
    assert old["observed_at"] == iso(instant)
    assert ledger.revision() == 0
    assert len(ledger.snapshots(node_id="other")) == 1
    assert len(ledger.snapshots(project_id="other")) == 1
    assert "created_at" not in original
    old["entries"].clear()
    assert ledger.snapshots("project", "node")[-1]["entries"]


def test_owned_upsert_preserves_other_entries_and_protected_policy(ledger):
    entries = [{"path": "a", "sha256": "a1", "stat": {"size": 1}},
               {"path": "b", "sha256": "b1", "stat": {"size": 2}, "policy": "keep"}]
    ledger.record_owned("project", "node", "/output", entries, kind="output", owner_id="run1",
                        source_node="source", source_root="/source")
    assert ledger.revision() == 1
    assert ledger.owned()[0]["policy"] == "protected"
    ledger.record_owned("project", "node", "/output", [{"path": "b", "sha256": "b2", "stat": {}}],
                        kind="output", owner_id="run2")
    assert ledger.revision() == 2
    data = ledger.owned("project", "node", "/output")
    assert len(data) == 2
    assert data[0]["sha256"] == "a1"
    assert data[0]["source_node"] == "source"
    assert data[1]["policy"] == "keep"
    assert data[1]["owner_id"] == "run2"
    assert data[1]["sha256"] == "b2"
    ledger.record_owned("other", "node", "/output", [{"path": "b", "sha256": "b3", "stat": {}}],
                        kind="input", owner_id="run3")
    assert len(ledger.owned()) == 2
    assert len(ledger.owned("project")) == 1
    assert len(ledger.owned("other")) == 1
    assert ledger.owned(root="/missing") == []
    assert ledger.owned(node_id="missing") == []
    assert "owner_id" not in entries[0]


def test_identical_owned_write_does_not_change_revision(ledger):
    entries = [{"path": "a", "sha256": "same", "stat": {}}]
    ledger.record_owned("p", "n", "/r", entries, kind="code", owner_id="run")
    ledger.record_owned("p", "n", "/r", entries, kind="code", owner_id="run")
    ledger.record_owned("p", "n", "/r", [], kind="code", owner_id="run")
    assert ledger.revision() == 1


def test_pin_revision_and_plan_captures_protection_revision(ledger):
    plan_id = ledger.save_plan({"files": []})
    assert ledger.get_plan(plan_id)["revision"] == 0
    pinned = ledger.set_pin("project", "run", True, "publication")
    assert pinned["pinned"] is True
    assert ledger.revision() == 1
    assert ledger.set_pin("project", "run", True, "publication") == pinned
    assert ledger.revision() == 1
    assert ledger.get_plan(plan_id)["revision"] != ledger.revision()
    ledger.set_pin("project", "run", False, "released")
    assert ledger.revision() == 2
    assert ledger.pins("project")[0]["pinned"] is False
    assert ledger.pins("missing") == []
    ledger.set_pin("project", "run", False, "updated reason")
    assert ledger.revision() == 3


def test_receipt_and_plan_roundtrip(ledger):
    receipt = {"receipt_id": "receipt1", "project_id": "project", "run_id": "run",
               "source_node": "source", "source_root": "/output",
               "dest_node": "target", "dest_root": "/archive", "complete": True, "verified": True,
               "entries": [{"path": "result.csv", "sha256": "digest", "stat": {"size": 100}}],
               "hashes": {"result.csv": "digest"}, "custom": {"method": "sha256"}}
    ledger.record_copy(receipt)
    stored = ledger.copies("project", "run")[0]
    assert {key: stored[key] for key in receipt} == receipt
    assert ledger.copies(run_id="run") == [stored]
    assert ledger.copies(project_id="missing") == []
    assert ledger.copies(run_id="missing") == []
    assert "created_at" not in receipt
    ledger.record_copy({**receipt, "verified": False})
    assert len(ledger.copies()) == 1
    assert ledger.copies()[0]["verified"] is False
    assert ledger.revision() == 2
    plan = {"plan_id": "plan1", "revision": 1, "entries": receipt["entries"],
            "evidence": receipt, "status": "reviewed", "unknown_field": ["preserve"]}
    assert ledger.save_plan(plan) == "plan1"
    assert {key: ledger.get_plan("plan1")[key] for key in plan} == plan
    ledger.update_plan("plan1", "committed", {"deleted": ["result.csv"], "complete": True})
    changed = ledger.get_plan("plan1")
    assert changed["revision"] == 1
    assert changed["evidence"] == receipt
    assert changed["status"] == "committed"
    assert changed["report"]["complete"] is True
    assert ledger.revision() == 2
    assert ledger.get_plan("missing") is None
    with pytest.raises(NotFound):
        ledger.update_plan("missing", "failed")
    assert "created_at" not in plan


def test_journal_is_idempotent_and_tracks_latest_path_state(ledger):
    ledger.journal("plan", "a", "intent", {"sha256": "abc"})
    first = ledger.journal_entries("plan")
    ledger.journal("plan", "a", "intent", {"sha256": "abc"})
    assert ledger.journal_entries("plan") == first
    ledger.journal("plan", "a", "deleted", {"verified": True})
    ledger.journal("plan", "b", "uncertain")
    ledger.journal("other", "a", "intent")
    entries = ledger.journal_entries("plan")
    assert len(entries) == 2
    assert entries[0]["status"] == "deleted"
    assert entries[0]["detail"] == {"verified": True}
    assert entries[1]["detail"] is None
    assert ledger.journal_entries("missing") == []
    assert ledger.revision() == 0


@pytest.mark.parametrize(("left", "right"), [
    ("/work/output", "/work/output/sub"), ("/work/output/sub", "/work/output"),
    ("/", "/anywhere"), ("/work/./output/", "/work/output/sub"),
    ("/work/unused/../output", "/work/output"),
    ("C:/Work/Output", "c:\\work\\output\\sub"),
    ("C:/WORK/output/sub", "c:/work/output"),
    ("//work/output", "/work/output"),
])
def test_guard_ancestor_overlap(ledger, left, right):
    guard_id = ledger.acquire_guard("node", [left], operation="commit", owner_id="plan")
    with pytest.raises(Conflict):
        ledger.acquire_guard("node", [right], operation="copy", owner_id="run")
    assert len(ledger.guards("node")) == 1
    assert ledger.guards()[0]["guard_id"] == guard_id
    assert ledger.revision() == 0


@pytest.mark.parametrize(("left", "right"), [
    ("/work/run", "/work/runtime"), ("/work/a", "/work/a-b"),
    ("/work/Run", "/work/run"), ("C:/work/run", "c:/work/runtime"),
    ("C:/work/run", "D:/work/run"), ("/C:/work/run", "C:/work/run"),
])
def test_guard_safe_siblings_and_path_flavors(ledger, left, right):
    ledger.acquire_guard("node", [left], operation="commit", owner_id="one")
    ledger.acquire_guard("node", [right], operation="copy", owner_id="two")
    assert len(ledger.guards("node")) == 2


def test_guard_release_and_node_isolation(ledger):
    first = ledger.acquire_guard("one", ["/work", "/other"], operation="commit", owner_id="plan")
    ledger.acquire_guard("two", ["/work"], operation="copy", owner_id="run")
    with pytest.raises(Conflict):
        ledger.acquire_guard("one", ["/safe", "/other/nested"], operation="copy", owner_id="run")
    ledger.release_guard(first)
    ledger.release_guard(first)
    ledger.release_guard("missing")
    assert ledger.guards("one") == []
    assert len(ledger.guards("two")) == 1
    ledger.acquire_guard("one", ["/other"], operation="copy", owner_id="run")


@pytest.mark.parametrize("roots", [[], "not-a-list", ["relative"], ["C:relative"], [""]])
def test_guard_rejects_ambiguous_roots(ledger, roots):
    with pytest.raises(SpecError):
        ledger.acquire_guard("node", roots, operation="commit", owner_id="plan")
    assert ledger.guards() == []


def test_two_connections_contend_atomically_and_retained_across_reopen(tmp_path):
    path = tmp_path / "contention.db"
    stores = [Store(path), Store(path)]
    ledgers = [Ledger(store) for store in stores]
    gate = Barrier(2)

    def acquire(value):
        gate.wait(timeout=10)
        try:
            return value.acquire_guard("node", ["/output"], operation="commit", owner_id="plan")
        except Conflict:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, ledgers))
        assert sum(item is not None for item in results) == 1
        assert len(ledgers[0].guards()) == 1
        assert ledgers[0].guards() == ledgers[1].guards()
        ledgers[0].set_pin("project", "run1", True, "one")
        ledgers[1].set_pin("project", "run2", True, "two")
        assert ledgers[0].revision() == ledgers[1].revision() == 2
    finally:
        for store in stores:
            store.close()
    store = Store(path)
    try:
        value = Ledger(store)
        assert value.guards()[0]["guard_id"] in results
        with pytest.raises(Conflict):
            value.acquire_guard("node", ["/output/sub"], operation="delete", owner_id="other")
    finally:
        store.close()


def test_guard_context_preserves_uncertain_work(ledger):
    with ledger.guard("node", ["/normal"], operation="copy", owner_id="run") as guard_id:
        assert ledger.guards()[0]["guard_id"] == guard_id
    assert ledger.guards() == []
    with pytest.raises(RuntimeError), ledger.guard("node", ["/uncertain"], operation="delete", owner_id="plan"):
        raise RuntimeError("missing acknowledgement")
    assert ledger.guards()[0]["roots"] == ["/uncertain"]
    assert ledger.revision() == 0


def test_nested_store_transaction_is_not_committed_or_rolled_back(tmp_path):
    store = Store(tmp_path / "nested.db")
    try:
        value = Ledger(store)
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute("INSERT INTO schema_meta VALUES ('caller', 'uncommitted')")
        with pytest.raises(Conflict):
            value.acquire_guard("node", ["/work"], operation="commit", owner_id="plan")
        assert store._conn.in_transaction
        assert store._conn.execute("SELECT value FROM schema_meta WHERE key='caller'").fetchone()[0] == "uncommitted"
        store._conn.rollback()
        assert value.guards() == []
    finally:
        store.close()


def test_bad_json_rolls_back_without_changing_revision(ledger):
    with pytest.raises(SpecError):
        ledger.record_copy({"project_id": "project", "value": object()})
    assert ledger.copies() == []
    assert ledger.revision() == 0
    assert json.loads(json.dumps(ledger.snapshots())) == []
