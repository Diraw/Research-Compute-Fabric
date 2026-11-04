"""Templates for real remote submission and cancellation.

These are skipped unless the operator opts in with all of:

    RCF_IT=1
    RCF_IT_ALLOW_SUBMIT=1
    RCF_IT_SUBMIT_NODE=<declared node>
    RCF_IT_PROJECT=<project with a path on that node>

The job is a plain ``sleep`` through ``command_override``; it does not run the
project's entrypoint and writes nothing into the project directory.  That keeps
the cost bounded and avoids touching research artifacts.
"""

from __future__ import annotations

import time

import pytest
from _support import enabled, make_service, submit_context

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not enabled(), reason="set RCF_IT=1 to run integration tests"),
]

TERMINAL = {"succeeded", "failed", "cancelled", "lost", "unknown"}


@pytest.fixture(scope="module")
def service():
    svc = make_service()
    yield svc
    svc.close()


def _wait_for(service, run_id: str, states: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    payload = service.get_run(run_id)
    while payload["state"] not in states and time.monotonic() < deadline:
        time.sleep(1)
        payload = service.get_run(run_id)
    return payload


def test_remote_sleep_completes(service):
    node, project, seconds = submit_context()

    plan = service.plan_run(
        {
            "project_id": project,
            "node_id": node,
            "command_override": f"sleep {seconds}",
            "requirements": {"cpus": 1, "time_minutes": max(2, int(seconds // 60) + 2)},
        }
    )
    assert not [check for check in plan["checks"] if check["status"] == "fail"]

    record = service.submit_run(plan["plan_id"], confirm=True)
    payload = _wait_for(service, record["run_id"], TERMINAL, timeout=seconds + 120)

    assert payload["state"] == "succeeded", payload
    # A command override is explicitly outside the project's artifact contract.
    assert payload["contract_violation"] is False
    assert "output_contract_unchecked" in {event["kind"] for event in payload["events"]}


def test_remote_command_can_be_cancelled(service):
    node, project, _ = submit_context()

    plan = service.plan_run(
        {
            "project_id": project,
            "node_id": node,
            "command_override": "sleep 300",
            "requirements": {"cpus": 1, "time_minutes": 10},
        }
    )
    assert not [check for check in plan["checks"] if check["status"] == "fail"]

    record = service.submit_run(plan["plan_id"], confirm=True)
    run_id = record["run_id"]
    _wait_for(service, run_id, {"running", "pending", "unknown"}, timeout=60)

    cancelled = service.cancel_run(run_id, confirm=True)
    assert cancelled["state"] in {"cancelled", "failed", "lost"}, cancelled
    assert cancelled["state"] != "running"
    assert run_id not in service.store.active_leases(node).values()
