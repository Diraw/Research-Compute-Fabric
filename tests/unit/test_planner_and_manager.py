"""Planning and run management with a controlled resource snapshot."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import NotFound, PermissionDenied
from rcf.core.models import (
    Check,
    CpuInfo,
    GpuInfo,
    GpuProcess,
    MemoryInfo,
    ResourceSnapshot,
    SchedulerView,
)
from rcf.core.runner import SubprocessRunner
from rcf.resources.probes import ProbeOutcome
from rcf.service import RcfService

TRAIN_PY = """import argparse
import os
import pathlib
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=0)
parser.add_argument("--seconds", type=float, default=0)
args = parser.parse_args()
if args.seconds:
    time.sleep(args.seconds)

out = pathlib.Path(os.environ["RCF_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "result.txt").write_text("seed=%d" % args.seed, encoding="utf-8")
print("done", args.seed)
"""


@pytest.fixture
def project_dir(tmp_path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    (path / "train.py").write_text(TRAIN_PY, encoding="utf-8")
    return path


@pytest.fixture
def svc(tmp_path, project_dir) -> RcfService:
    home = make_home(
        tmp_path,
        nodes="""\
[nodes.laptop]
kind = "local"
labels = { site = "home" }
""",
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}} --seconds {{seconds}}"
output_contract = "env"
script = "train.py"
outputs = "outputs"
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner(), actor="test")
    yield service
    service.close()


def gpu(index, uuid, used_mb=0, procs=0) -> GpuInfo:
    return GpuInfo(
        index=index,
        uuid=uuid,
        name="NVIDIA A100",
        memory_total_mb=40960,
        memory_used_mb=used_mb,
        processes=[GpuProcess(pid=1000 + index, name="python", used_memory_mb=used_mb) for _ in range(procs)],
    )


def snapshot(gpus=None, node_id="laptop", cpus=16, mem_available=64000) -> ResourceSnapshot:
    return ResourceSnapshot(
        node_id=node_id,
        observed_at=datetime.now(UTC),
        adapter="local",
        home_dir=str(Path.home()),
        cpu=CpuInfo(logical_cores=cpus, load_1m=0.5),
        memory=MemoryInfo(total_mb=mem_available + 1000, available_mb=mem_available),
        gpus=gpus or [],
        scheduler=SchedulerView(available=False),
        checks=[Check(name="host", status="ok", detail="fake")],
    )


def use_snapshot(service: RcfService, snap: ResourceSnapshot) -> None:
    service.inspector.probe = lambda node: ProbeOutcome(snapshot=snap)  # type: ignore[assignment]
    service.inspector.invalidate()


def wait_state(service: RcfService, run_id: str, states: set[str], timeout: float = 20.0):
    deadline = time.time() + timeout
    record = service.get_run(run_id)
    while time.time() < deadline and record["state"] not in states:
        time.sleep(0.2)
        record = service.get_run(run_id)
    return record


def test_plan_for_local_project(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    assert plan["executor"] == "local"
    assert plan["node_id"] == "laptop"
    assert plan["command"]["argv"][-4:] == ["--seed", "1", "--seconds", "0"]
    assert not [c for c in plan["checks"] if c["status"] == "fail"]
    assert plan["command"]["env"]["RCF_RUN_ID"] == plan["run_id"]
    assert plan["result_dir"].endswith("outputs")


def test_plan_reports_missing_placeholder(svc):
    from rcf.core.errors import SpecError

    with pytest.raises(SpecError, match="unknown placeholder"):
        svc.plan_run({"project_id": "demo", "params": {"seed": 1}})


def test_unknown_project_is_not_found(svc):
    with pytest.raises(NotFound):
        svc.plan_run({"project_id": "ghost"})


def test_plan_selects_idle_gpu_and_pins_it(svc):
    use_snapshot(
        svc,
        snapshot([gpu(0, "GPU-0"), gpu(1, "GPU-1", used_mb=20000, procs=1)]),
    )
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 0}, "requirements": {"gpus": 1}}
    )
    assert plan["gpu_leases"] == ["GPU-0"]
    assert plan["command"]["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert not [c for c in plan["checks"] if c["status"] == "fail"]


def test_plan_blocks_when_all_gpus_are_busy(svc):
    use_snapshot(svc, snapshot([gpu(0, "GPU-0", used_mb=20000, procs=1)]))
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 0}, "requirements": {"gpus": 1}}
    )
    assert [c["name"] for c in plan["checks"] if c["status"] == "fail"] == ["gpu_availability"]
    with pytest.raises(PermissionDenied):
        svc.submit_run(plan["plan_id"], confirm=True)


def test_plan_allows_explicit_gpu_sharing_when_vram_fits(svc):
    use_snapshot(svc, snapshot([gpu(0, "GPU-0", used_mb=20000, procs=1)]))
    plan = svc.plan_run(
        {
            "project_id": "demo",
            "params": {"seed": 1, "seconds": 0},
            "requirements": {"gpus": 1, "gpu_memory_gb": 8, "allow_shared_gpu": True},
        }
    )
    assert not [c for c in plan["checks"] if c["status"] == "fail"]
    assert any("sharing" in w for w in plan["warnings"])


def test_plan_blocks_gpu_held_by_another_rcf_run(svc):
    use_snapshot(svc, snapshot([gpu(0, "GPU-0"), gpu(1, "GPU-1")]))
    svc.store.acquire_leases(run_id="run_other", node_id="laptop", keys=["GPU-0", "GPU-1"])
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 0}, "requirements": {"gpus": 1}}
    )
    assert [c["name"] for c in plan["checks"] if c["status"] == "fail"] == ["gpu_availability"]
    detail = next(c["detail"] for c in plan["checks"] if c["name"] == "gpu_availability")
    assert "reserved by RCF run" in detail


def test_submit_requires_confirmation(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    with pytest.raises(PermissionDenied, match="confirmation"):
        svc.submit_run(plan["plan_id"], confirm=False)


def test_submit_denied_when_execute_is_disabled(tmp_path, project_dir):
    home = make_home(
        tmp_path,
        settings="[security]\nallow_execute = false\n",
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}} --seconds {{seconds}}"
output_contract = "env"
script = "train.py"
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())
    plan = service.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    with pytest.raises(PermissionDenied, match="disabled"):
        service.submit_run(plan["plan_id"], confirm=True)
    service.close()


def test_submit_runs_and_releases_lease(svc):
    use_snapshot(svc, snapshot([gpu(0, "GPU-0")]))
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 4, "seconds": 0}, "requirements": {"gpus": 1}}
    )
    record = svc.submit_run(plan["plan_id"], confirm=True)
    assert svc.store.active_leases("laptop") == {"GPU-0": record["run_id"]}

    done = wait_state(svc, record["run_id"], {"succeeded", "failed"})
    assert done["state"] == "succeeded"
    assert done["exit_code"] == 0
    assert svc.store.active_leases("laptop") == {}


def test_second_plan_is_blocked_while_lease_is_held(svc):
    use_snapshot(svc, snapshot([gpu(0, "GPU-0")]))
    first = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 20}, "requirements": {"gpus": 1}}
    )
    record = svc.submit_run(first["plan_id"], confirm=True)
    try:
        second = svc.plan_run(
            {"project_id": "demo", "params": {"seed": 2, "seconds": 0}, "requirements": {"gpus": 1}}
        )
        assert [c["name"] for c in second["checks"] if c["status"] == "fail"] == ["gpu_availability"]
        with pytest.raises(PermissionDenied):
            svc.submit_run(second["plan_id"], confirm=True)
    finally:
        svc.cancel_run(record["run_id"], confirm=True)


def test_submit_conflicts_when_a_lease_is_taken_after_planning(svc):
    """The race the DB constraint exists for: two Agents plan at the same time."""

    from rcf.core.errors import Conflict

    use_snapshot(svc, snapshot([gpu(0, "GPU-0")]))
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 0}, "requirements": {"gpus": 1}}
    )
    svc.store.acquire_leases(run_id="run_other", node_id="laptop", keys=["GPU-0"])
    with pytest.raises(Conflict) as excinfo:
        svc.submit_run(plan["plan_id"], confirm=True)
    assert excinfo.value.detail["held_by"]["GPU-0"] == "run_other"


def test_cancel_local_run(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 30}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    time.sleep(0.5)
    cancelled = svc.cancel_run(record["run_id"], confirm=True)
    assert cancelled["state"] == "cancelled"
    assert svc.store.active_leases("laptop") == {}


def test_failed_run_records_exit_code(svc, project_dir):
    (project_dir / "train.py").write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    done = wait_state(svc, record["run_id"], {"succeeded", "failed"})
    assert done["state"] == "failed"
    assert done["exit_code"] == 7


def test_results_manifest_lists_outputs(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 9, "seconds": 0}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})
    manifest = svc.collect_results(record["run_id"])
    assert [entry["path"] for entry in manifest["entries"]] == ["result.txt"]
    assert (Path(manifest["result_dir"]) / "result.txt").read_text(encoding="utf-8") == "seed=9"


def test_logs_are_persisted(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 11, "seconds": 0}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})
    logs = svc.get_logs(record["run_id"])
    assert "done 11" in logs["log"]


def test_events_trail_is_recorded(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})
    events = svc.runs.events(record["run_id"])
    assert events[0].kind == "submitted"
    assert any(e.kind == "state" for e in events)


def test_transport_failure_during_submit_keeps_run_unknown(svc, monkeypatch):
    """A dropped link must not be reported as a clean failure (the job may exist)."""

    from rcf.core.errors import HostUnreachable
    from rcf.core.models import RunState

    use_snapshot(svc, snapshot([gpu(0, "GPU-0")]))
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 0}, "requirements": {"gpus": 1}}
    )

    class BrokenExecutor:
        kind = "local"
        node_id = "laptop"

        def submit(self, spec):
            raise HostUnreachable("ssh: connect to host laptop: Connection refused")

    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a, **k: BrokenExecutor())
    with pytest.raises(HostUnreachable):
        svc.submit_run(plan["plan_id"], confirm=True)

    record = svc.store.get_run(plan["run_id"])
    assert record.state is RunState.UNKNOWN
    assert "may exist" in (record.error or "")
    # The lease must stay: the remote job might still be using the GPU.
    assert svc.store.active_leases("laptop") == {"GPU-0": record.run_id}


def test_clean_submit_rejection_is_marked_failed(svc, monkeypatch):
    from rcf.core.errors import SchedulerError
    from rcf.core.models import RunState

    use_snapshot(svc, snapshot([gpu(0, "GPU-0")]))
    plan = svc.plan_run(
        {"project_id": "demo", "params": {"seed": 1, "seconds": 0}, "requirements": {"gpus": 1}}
    )

    class RejectingExecutor:
        kind = "local"
        node_id = "laptop"

        def submit(self, spec):
            raise SchedulerError("sbatch: Invalid partition name")

    monkeypatch.setattr("rcf.runs.manager.build_executor", lambda *a, **k: RejectingExecutor())
    with pytest.raises(SchedulerError):
        svc.submit_run(plan["plan_id"], confirm=True)

    record = svc.store.get_run(plan["run_id"])
    assert record.state is RunState.FAILED
    assert svc.store.active_leases("laptop") == {}


def test_python_placeholder_defaults_to_python3_on_posix(svc):
    """Ubuntu 24.04+ has no bare `python`; the default must match the platform."""

    import os

    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    assert plan["command"]["argv"][0] == ("python" if os.name == "nt" else "python3")


def test_unverifiable_project_path_blocks_submission(svc, monkeypatch):
    """'unknown' is not a licence to submit: `cd` would fail immediately."""

    from rcf.core.models import ProbeStatus

    monkeypatch.setattr(
        "rcf.core.nodeops.NodeOps.dir_exists",
        lambda self, node, path: ProbeStatus.UNKNOWN,
    )
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
    assert [c["name"] for c in plan["checks"] if c["status"] == "fail"] == ["project_path"]
    with pytest.raises(PermissionDenied):
        svc.submit_run(plan["plan_id"], confirm=True)


def test_launch_failure_can_be_retried_with_the_same_plan(svc, monkeypatch):
    """No handle means no job exists, so fixing the cause and retrying is safe."""

    from rcf.core.errors import ExecutorError
    from rcf.executors import factory

    real = factory.build_executor
    calls = {"count": 0}

    def flaky(config, node, runner):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ExecutorError("failed to launch run: simulated")
        return real(config, node, runner)

    monkeypatch.setattr("rcf.runs.manager.build_executor", flaky)
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 2, "seconds": 0}})

    with pytest.raises(ExecutorError):
        svc.submit_run(plan["plan_id"], confirm=True)

    record = svc.submit_run(plan["plan_id"], confirm=True)
    assert record["run_id"] == plan["run_id"]
    assert record["state"] in {"pending", "running", "succeeded"}
    assert record["handle"] is not None


def test_local_results_report_workdir_artifacts(svc, project_dir):
    """Same diagnostic for the local executor (pathlib implementation)."""

    (project_dir / "side.py").write_text(
        "open('side_effect.txt', 'w').write('x')\n", encoding="utf-8"
    )
    plan = svc.plan_run({"project_id": "demo", "command_override": "{python} side.py"})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})

    manifest = svc.collect_results(record["run_id"])
    assert manifest["entries"] == []
    assert any(e["path"] == "side_effect.txt" for e in manifest["workdir_changes"])


def test_local_fetch_copies_results(svc, tmp_path):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 9, "seconds": 0}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})

    dest = tmp_path / "local-fetch"
    payload = svc.fetch_results(record["run_id"], dest=str(dest), confirm=True)
    assert payload["files"] == 1
    assert payload["verified"] is True
    assert (dest / "result.txt").read_text(encoding="utf-8") == "seed=9"


def _plan_with_contract(svc, monkeypatch, contract: str):
    from rcf.core.models import OutputContract

    project = svc.config.project("demo")
    monkeypatch.setattr(
        svc.config, "project", lambda pid: project.model_copy(update={"output_contract": OutputContract(contract)})
    )
    return svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})


def test_output_contract_is_enforced_by_default(tmp_path, project_dir, monkeypatch):
    """A run whose artifacts cannot be found is a run you cannot reproduce."""

    home = make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}}"
script = "train.py"
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())
    try:
        plan = service.plan_run({"project_id": "demo", "params": {"seed": 1}})
        assert [c["name"] for c in plan["checks"] if c["status"] == "fail"] == ["output_contract"]
        with pytest.raises(PermissionDenied):
            service.submit_run(plan["plan_id"], confirm=True)
    finally:
        service.close()


def test_output_contract_argument_passes_when_template_has_output_dir(svc, monkeypatch):
    plan = _plan_with_contract(svc, monkeypatch, "argument")
    # The demo entrypoint has no {output_dir}, so it is still a violation.
    assert [c["name"] for c in plan["checks"] if c["status"] == "fail"] == ["output_contract"]


def test_output_contract_env_is_accepted(svc, monkeypatch):
    plan = _plan_with_contract(svc, monkeypatch, "env")
    check = next(c for c in plan["checks"] if c["name"] == "output_contract")
    assert check["status"] == "ok"
    assert not [c for c in plan["checks"] if c["status"] == "fail"]


def test_output_contract_none_only_warns(svc, monkeypatch):
    plan = _plan_with_contract(svc, monkeypatch, "none")
    check = next(c for c in plan["checks"] if c["name"] == "output_contract")
    assert check["status"] == "warn"
    assert not [c for c in plan["checks"] if c["status"] == "fail"]


def test_command_override_downgrades_the_contract_check(svc):
    plan = svc.plan_run(
        {"project_id": "demo", "command_override": "{python} train.py --seed 1"}
    )
    check = next(c for c in plan["checks"] if c["name"] == "output_contract")
    assert check["status"] == "warn"


def test_output_contract_default_is_satisfied_by_passing_output_dir(tmp_path, project_dir):
    home = make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}} --out {{output_dir}}"
script = "train.py"
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())
    try:
        plan = service.plan_run({"project_id": "demo", "params": {"seed": 1}})
        check = next(c for c in plan["checks"] if c["name"] == "output_contract")
        assert check["status"] == "ok"
        assert not [c for c in plan["checks"] if c["status"] == "fail"]
    finally:
        service.close()


def test_run_without_artifacts_is_flagged(svc, project_dir):
    """The contract is verified after the fact, not just in the template."""

    (project_dir / "silent.py").write_text("print('nothing produced')\n", encoding="utf-8")
    plan = svc.plan_run({"project_id": "demo", "command_override": "{python} silent.py"})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    done = wait_state(svc, record["run_id"], {"succeeded", "failed"})

    assert done["state"] == "succeeded"
    assert done["contract_violation"] is True
    events = [e.kind for e in svc.runs.events(record["run_id"])]
    assert "output_contract_violation" in events


def test_compliant_run_is_not_flagged(svc):
    plan = svc.plan_run({"project_id": "demo", "params": {"seed": 3, "seconds": 0}})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    done = wait_state(svc, record["run_id"], {"succeeded", "failed"})
    assert done["state"] == "succeeded"
    assert done["contract_violation"] is False


def test_contract_none_is_not_flagged(svc, project_dir, monkeypatch):
    from rcf.core.models import OutputContract

    (project_dir / "silent.py").write_text("print('nothing produced')\n", encoding="utf-8")
    project = svc.config.project("demo")
    monkeypatch.setattr(
        svc.config,
        "project",
        lambda pid: project.model_copy(update={"output_contract": OutputContract.NONE}),
    )
    plan = svc.plan_run({"project_id": "demo", "command_override": "{python} silent.py"})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    done = wait_state(svc, record["run_id"], {"succeeded", "failed"})
    assert done["contract_violation"] is False


def _git(args, cwd):
    import subprocess

    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def test_git_commit_is_captured_and_persisted(tmp_path, project_dir):
    _git(["init", "-q"], project_dir)
    (project_dir / ".gitignore").write_text("outputs/\n", encoding="utf-8")
    _git(["add", "-A"], project_dir)
    _git(["-c", "commit.gpgsign=false", "commit", "-qm", "initial"], project_dir)

    home = make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}} --seconds {{seconds}}"
output_contract = "env"
script = "train.py"
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())
    try:
        plan = service.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        assert plan["git_commit"] and len(plan["git_commit"]) == 40
        assert plan["git_dirty"] is False

        record = service.submit_run(plan["plan_id"], confirm=True)
        assert record["git_commit"] == plan["git_commit"]
        assert record["git_dirty"] is False
    finally:
        service.close()


def test_dirty_tree_is_recorded(tmp_path, project_dir):
    _git(["init", "-q"], project_dir)
    _git(["add", "-A"], project_dir)
    _git(["-c", "commit.gpgsign=false", "commit", "-qm", "initial"], project_dir)
    (project_dir / "train.py").write_text("print('changed')\n", encoding="utf-8")

    home = make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}} --seconds {{seconds}}"
output_contract = "env"
script = "train.py"
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())
    try:
        plan = service.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        assert plan["git_dirty"] is True
    finally:
        service.close()


def _slurm_service(tmp_path, project_dir):
    from rcf.core.models import SchedulerView, SlurmPartition

    home = make_home(
        tmp_path,
        nodes="""\
[nodes.hpc]
kind = "slurm"
ssh_alias = "hpc"
work_root = "/public/home/me/rcf"

[nodes.hpc.ssh]
program = ["/bin/ssh"]

[nodes.hpc.slurm]
default_partition = "debug"
partitions = ["debug", "normal", "gpu"]
""",
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}}"
output_contract = "env"
script = "train.py"
paths = {{ "hpc" = "{toml_path(project_dir)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())
    snap = snapshot(node_id="hpc")
    snap.scheduler = SchedulerView(
        available=True,
        partitions=[
            SlurmPartition(name="debug", state="up", avl_nodes=2, total_nodes=18),
            SlurmPartition(
                name="gpu", state="up", avl_nodes=2, total_nodes=2, gres="gpu:RTX3090:8"
            ),
        ],
    )
    service.inspector.probe = lambda node: ProbeOutcome(snapshot=snap)  # type: ignore[assignment]
    service.inspector.invalidate()

    from rcf.core.models import ProbeStatus

    class StubOps:
        """No network: these tests are about partition choice, not the link."""

        def dir_exists(self, node, path):
            return ProbeStatus.OK

        def git_state(self, node, path):
            return {"rcf_repo": "git", "rcf_commit": "a" * 40, "rcf_dirty": "no"}

    service.planner.ops = StubOps()  # type: ignore[assignment]
    return service


def test_gpu_request_chooses_a_gpu_partition(tmp_path, project_dir):
    service = _slurm_service(tmp_path, project_dir)
    try:
        plan = service.plan_run(
            {"project_id": "demo", "params": {"seed": 1}, "requirements": {"gpus": 1}}
        )
        assert plan["partition"] == "gpu"
        assert any(c["name"] == "partition_choice" for c in plan["checks"])
        assert not [c for c in plan["checks"] if c["status"] == "fail"]
    finally:
        service.close()


def test_cpu_request_keeps_the_default_partition(tmp_path, project_dir):
    service = _slurm_service(tmp_path, project_dir)
    try:
        plan = service.plan_run({"project_id": "demo", "params": {"seed": 1}})
        assert plan["partition"] == "debug"
        assert not any(c["name"] == "partition_choice" for c in plan["checks"])
    finally:
        service.close()


def test_explicit_partition_is_respected(tmp_path, project_dir):
    service = _slurm_service(tmp_path, project_dir)
    try:
        plan = service.plan_run(
            {
                "project_id": "demo",
                "params": {"seed": 1},
                "requirements": {"gpus": 1, "partition": "normal"},
            }
        )
        assert plan["partition"] == "normal"
    finally:
        service.close()


def test_environment_can_be_chosen_per_node(tmp_path, project_dir):
    """One project, two machines, two different environment mechanisms."""

    from rcf.core.models import ProbeStatus

    home = make_home(
        tmp_path,
        nodes="""\
[nodes.hpc]
kind = "slurm"
ssh_alias = "hpc"
work_root = "/public/home/me/rcf"

[nodes.hpc.ssh]
program = ["/bin/ssh"]

[nodes.hpc.slurm]
default_partition = "debug"
partitions = ["debug"]

[nodes.ws]
kind = "ssh"
ssh_alias = "ws"
work_root = "/home/me/rcf"

[nodes.ws.ssh]
program = ["/bin/ssh"]
""",
        projects="""\
[projects.demo]
entrypoint = "{python} {script} --seed {seed}"
output_contract = "env"
script = "train.py"
env_by_node = { "hpc" = "hpc-modules" }
paths = { "hpc" = "/public/home/me/proj", "ws" = "/home/me/proj" }
""",
        environments="""\
[environments.hpc-modules]
kind = "module"
python = "python3"
modules = ["compiler/python/3.12.6"]
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner())

    class StubOps:
        def dir_exists(self, node, path):
            return ProbeStatus.OK

        def git_state(self, node, path):
            return {"rcf_repo": "no_git"}

    from rcf.core.models import SchedulerView, SlurmPartition

    snap = snapshot(node_id="hpc")
    snap.scheduler = SchedulerView(
        available=True, partitions=[SlurmPartition(name="debug", avl_nodes=2, total_nodes=18)]
    )
    service.inspector.probe = lambda node: ProbeOutcome(snapshot=snap)  # type: ignore[assignment]
    service.inspector.invalidate()
    service.planner.ops = StubOps()  # type: ignore[assignment]

    from rcf.core.models import EnvironmentState, EnvironmentView

    service.environments.inspect = lambda env, node, verify=False: EnvironmentView(  # type: ignore[assignment]
        env_id=env.id, node_id=node.id, kind=env.kind, state=EnvironmentState.COMPATIBLE
    )
    try:
        plan = service.plan_run({"project_id": "demo", "params": {"seed": 1}, "node_id": "hpc"})
        assert plan["env_id"] == "hpc-modules"
        assert any("module load compiler/python/3.12.6" in line for line in plan["command"]["preamble"])
    finally:
        service.close()


def test_failure_diagnosis_names_the_missing_command(svc, project_dir):
    (project_dir / "broken.py").write_text(
        "import sys\nprint('run.sh: line 1: python3: command not found')\nsys.exit(127)\n",
        encoding="utf-8",
    )
    plan = svc.plan_run({"project_id": "demo", "command_override": "{python} broken.py"})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})

    diagnoses = [e for e in svc.runs.events(record["run_id"]) if e.kind == "diagnosis"]
    assert diagnoses, "no diagnosis event"
    assert "not on PATH" in diagnoses[0].message


def test_failure_diagnosis_recognises_oom(svc, project_dir):
    (project_dir / "oom.py").write_text("import sys\nsys.exit(137)\n", encoding="utf-8")
    plan = svc.plan_run({"project_id": "demo", "command_override": "{python} oom.py"})
    record = svc.submit_run(plan["plan_id"], confirm=True)
    wait_state(svc, record["run_id"], {"succeeded", "failed"})

    diagnoses = [e for e in svc.runs.events(record["run_id"]) if e.kind == "diagnosis"]
    assert diagnoses and "out of memory" in diagnoses[0].message
