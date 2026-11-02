"""Process death, not elapsed time, controls cleanup recovery eligibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from rcf.core.errors import Conflict
from rcf.core.ids import iso, now_utc
from rcf.runs.store import Store
from rcf.storage.execution import execution_lock
from rcf.storage.ledger import Ledger


def sample_plan(ledger):
    plan = {"plan_id": "cleanup_recovery", "action": "cleanup", "project_id": "demo",
            "node_id": "local", "root_id": "code", "root": "/synthetic",
            "entries": [{"path": "a", "sha256": "1" * 64}], "status": "planned"}
    ledger.save_plan(plan)
    return plan


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path / "state.db")
    yield Ledger(store)
    store.close()


def test_live_controller_lock_cannot_be_stolen_even_before_guard(tmp_path):
    with execution_lock(tmp_path, "same-plan"):
        with pytest.raises(Conflict, match="live process"):
            with execution_lock(tmp_path, "same-plan"):
                pytest.fail("second applier entered")


def test_two_threads_have_one_execution_owner(tmp_path):
    ready, leave = Barrier(2), Barrier(2)

    def first():
        with execution_lock(tmp_path, "plan"):
            ready.wait(timeout=10)
            leave.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        ready.wait(timeout=10)
        try:
            with pytest.raises(Conflict):
                with execution_lock(tmp_path, "plan"):
                    pass
        finally:
            leave.wait(timeout=10)
        future.result(timeout=10)


def test_process_crash_releases_os_execution_lock(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = (
        "from pathlib import Path; from rcf.storage.execution import execution_lock; "
        "import os,sys; "
        "\nwith execution_lock(Path(sys.argv[1]), 'crash-plan') as owner:\n"
        " print(owner['token'], flush=True)\n os._exit(0)\n"
    )
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    completed = subprocess.run([sys.executable, "-c", script, str(home)], capture_output=True,
                               text=True, timeout=20, **kwargs)
    assert completed.returncode == 0, completed.stderr
    with execution_lock(home, "crash-plan") as new:
        assert new["token"] != completed.stdout.strip()


def test_stale_execution_token_cannot_write_after_recovery(ledger, tmp_path):
    plan = sample_plan(ledger)
    with execution_lock(tmp_path, plan["plan_id"]) as old:
        ledger.claim_plan(plan["plan_id"], owner=old, ownership_verified=True)
        ledger.update_plan(plan["plan_id"], "unknown", owner_token=old["token"], stage="dispatching")
    with execution_lock(tmp_path, plan["plan_id"]) as new:
        claimed = ledger.claim_plan(plan["plan_id"], owner=new, ownership_verified=True,
                                    allow_resume=True, previous_token=old["token"])
        assert claimed["_previous_status"] == "unknown"
        with pytest.raises(Conflict, match="stale"):
            ledger.update_plan(plan["plan_id"], "completed", owner_token=old["token"])
        ledger.update_plan(plan["plan_id"], "completed", owner_token=new["token"])
        with pytest.raises(Conflict, match="immutable"):
            ledger.update_plan(plan["plan_id"], "planned", owner_token=new["token"])


def test_foreign_controller_cannot_take_over_even_with_old_heartbeat(ledger, tmp_path):
    plan = sample_plan(ledger)
    with execution_lock(tmp_path, plan["plan_id"]) as old:
        old["controller"] = "another-host"
        old["heartbeat_at"] = iso(now_utc() - timedelta(days=30))
        ledger.claim_plan(plan["plan_id"], owner=old)
        ledger.update_plan(plan["plan_id"], "unknown", owner_token=old["token"])
    with execution_lock(tmp_path, plan["plan_id"]) as new:
        with pytest.raises(Conflict, match="unverifiable"):
            ledger.claim_plan(plan["plan_id"], owner=new, ownership_verified=True,
                              allow_resume=True, previous_token=old["token"])


def test_recovery_claim_uses_compare_and_swap(ledger, tmp_path):
    plan = sample_plan(ledger)
    with execution_lock(tmp_path, plan["plan_id"]) as owner:
        ledger.claim_plan(plan["plan_id"], owner=owner)
        ledger.update_plan(plan["plan_id"], "unknown", owner_token=owner["token"])
        with pytest.raises(Conflict, match="changed"):
            ledger.claim_plan(plan["plan_id"], owner=owner, ownership_verified=True,
                              allow_resume=True, previous_token="outdated-token")


def test_plan_selection_cannot_be_overwritten_after_review(ledger):
    plan = sample_plan(ledger)
    with pytest.raises(Conflict, match="immutable"):
        ledger.save_plan({**plan, "entries": [{"path": "different-file"}]})
    assert ledger.get_plan(plan["plan_id"])["entries"] == plan["entries"]


def test_inspection_is_read_only_and_includes_execution_stage(ledger, tmp_path):
    plan = sample_plan(ledger)
    with execution_lock(tmp_path, plan["plan_id"]) as owner:
        ledger.claim_plan(plan["plan_id"], owner=owner)
        ledger.update_plan(plan["plan_id"], "unknown", owner_token=owner["token"], stage="outcome_unknown")
    before = ledger.get_plan(plan["plan_id"])
    assert before["execution"]["stage"] == "outcome_unknown"
    assert json.dumps(ledger.get_plan(plan["plan_id"]), sort_keys=True) == json.dumps(before, sort_keys=True)
