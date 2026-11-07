"""Real GPU check inside a Slurm allocation, opt-in.

This is the driver tier: it proves the allocation exposes a device and that a
context can allocate memory and round-trip bytes.  It does not run a kernel, and
it needs no CUDA wheel, cupy or torch — a separate, heavier claim.

The script must already be in the project checkout on the node (this test does not
copy files).  Upload it once with:

    scp tests/integration/assets/gpu_cuda_smoke.py <node>:<project>/gpu_cuda_smoke.py

Then:

    RCF_IT=1 RCF_IT_ALLOW_SUBMIT=1 RCF_IT_GPU=1 \\
    RCF_IT_SUBMIT_NODE=<node> RCF_IT_PROJECT=<project> \\
    pytest tests/integration/test_gpu_compute.py
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
GPU_SCRIPT = os.environ.get("RCF_IT_GPU_SCRIPT", "gpu_cuda_smoke.py")


def gpu_enabled() -> bool:
    return os.environ.get("RCF_IT_GPU", "").strip().lower() in {"1", "true", "yes", "on"}


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


@pytest.mark.skipif(not gpu_enabled(), reason="set RCF_IT_GPU=1 to submit a real GPU job")
def test_gpu_driver_round_trip(service):
    node, project, _ = submit_context()

    plan = service.plan_run(
        {
            "project_id": project,
            "node_id": node,
            "command_override": f"{{python}} {GPU_SCRIPT}",
            "requirements": {"gpus": 1, "cpus": 1, "memory_gb": 4, "time_minutes": 4},
        }
    )
    assert not [check for check in plan["checks"] if check["status"] == "fail"], plan["checks"]
    assert any(check["name"] == "gpu_request" for check in plan["checks"])
    assert plan["partition"], "a GPU request must land in a partition"

    record = service.submit_run(plan["plan_id"], confirm=True)
    payload = _wait_for(service, record["run_id"], TERMINAL, timeout=300)

    log = service.get_logs(record["run_id"], tail=40)["log"]
    assert payload["state"] == "succeeded", payload | {"log": log}
    # The script prints one JSON line; the assertions stay on observable evidence,
    # not on a message that a different driver version might word differently.
    assert '"ok": true' in log
    assert '"mismatches": 0' in log
    assert '"allocated_mb": 64' in log
