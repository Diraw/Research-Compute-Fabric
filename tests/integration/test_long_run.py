"""A long Slurm run across an observation gap, opt-in.

This encodes the procedure behind the 2026-11-07 acceptance (job 267670, 65
minutes on lab-hpc).  It is not a unit test with a fake clock: the point is that a
real job keeps running while the control side is offline, the saved claim goes
stale instead of turning into a guess, and one refresh after the gap recovers the
truth without losing the lease.

Requires ``sleep_heartbeat.py`` in the project checkout on the node:

    scp tests/integration/assets/sleep_heartbeat.py <node>:<project>/sleep_heartbeat.py

Then, with a project that declares a [progress] contract (long runs without one
are refused by design):

    RCF_IT=1 RCF_IT_ALLOW_SUBMIT=1 RCF_IT_LONG=1 \\
    RCF_IT_LONG_SECONDS=3900 RCF_IT_SUBMIT_NODE=<node> RCF_IT_PROJECT=<project> \\
    pytest tests/integration/test_long_run.py
"""

from __future__ import annotations

import os
import time

import pytest
from _support import enabled, make_service, submit_context

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not enabled(), reason="set RCF_IT=1 to run integration tests"),
]

TERMINAL = {"succeeded", "failed", "cancelled", "lost", "unknown"}
HEARTBEAT = os.environ.get("RCF_IT_LONG_SCRIPT", "sleep_heartbeat.py")
#: The gap has to exceed the staleness threshold, or ``stale`` proves nothing.
GAP_SECONDS = 420


def long_enabled() -> bool:
    return os.environ.get("RCF_IT_LONG", "").strip().lower() in {"1", "true", "yes", "on"}


def run_seconds() -> int:
    raw = os.environ.get("RCF_IT_LONG_SECONDS", "3900").strip()
    try:
        seconds = int(raw)
    except ValueError:
        pytest.fail(f"RCF_IT_LONG_SECONDS must be an integer, got {raw!r}")
    if seconds < 3600:
        pytest.fail("RCF_IT_LONG_SECONDS must be at least 3600; shorter runs are not long")
    return seconds


@pytest.fixture(scope="module")
def service():
    svc = make_service()
    yield svc
    svc.close()


@pytest.mark.skipif(not long_enabled(), reason="set RCF_IT_LONG=1 for a >=1 hour job")
def test_long_run_survives_an_observation_gap(service):
    node, project, _ = submit_context()
    if service.config.project(project).progress is None:
        pytest.skip("a >=1 hour run needs a project with a [progress] contract")
    seconds = run_seconds()

    plan = service.plan_run(
        {
            "project_id": project,
            "node_id": node,
            "command_override": f"{{python}} {HEARTBEAT} --seconds {seconds} --interval 60",
            "requirements": {
                "cpus": 1,
                "memory_gb": 2,
                "time_minutes": seconds // 60 + 10,
            },
        }
    )
    assert not [check for check in plan["checks"] if check["status"] == "fail"], plan["checks"]

    record = service.submit_run(plan["plan_id"], confirm=True)
    run_id = record["run_id"]
    started = time.monotonic()

    # One observation while it runs, then deliberately none at all.
    running = _wait_for(service, run_id, {"running"}, timeout=120)
    assert running["state"] == "running"
    time.sleep(GAP_SECONDS)

    stale = service.get_run(run_id, refresh=False)
    assert stale["state"] == "running"
    assert stale["stale"] is True, "an unobserved claim must not look fresh"
    assert stale["observed_age_seconds"] >= GAP_SECONDS

    recovered = service.get_run(run_id, refresh=True)
    assert recovered["state"] in {"running", "succeeded"}, recovered
    assert recovered["stale"] is False

    finished = _wait_for(service, run_id, TERMINAL, timeout=seconds + 900)
    elapsed = time.monotonic() - started
    assert finished["state"] == "succeeded", finished
    assert finished["exit_code"] == 0
    assert elapsed >= seconds, "the job cannot have finished before its own duration"
    assert run_id not in service.store.active_leases(node).values()

    log = service.get_logs(run_id, tail=5)["log"]
    assert "done after" in log
    kinds = {event["kind"] for event in finished["events"]}
    assert "state" in kinds and "observe_failed" not in kinds


def _wait_for(service, run_id: str, states: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    payload = service.get_run(run_id)
    while payload["state"] not in states and time.monotonic() < deadline:
        time.sleep(5)
        payload = service.get_run(run_id)
    return payload
