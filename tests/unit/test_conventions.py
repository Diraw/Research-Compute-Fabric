"""The three conventions: submission, inputs, environment locks.

These tests pin the *policy* decisions, not the implementation: a declared input
that is missing must block submission, a plan must expire, and an environment
lock must be a real package list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import NotFound, PlanExpired
from rcf.core.models import Check, CpuInfo, MemoryInfo, ResourceSnapshot, SchedulerView
from rcf.core.runner import CommandResult, SubprocessRunner
from rcf.resources.probes import ProbeOutcome
from rcf.service import RcfService

TRAIN_PY = """import argparse, os, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=0)
parser.add_argument("--seconds", type=float, default=0)
args = parser.parse_args()
out = pathlib.Path(os.environ["RCF_OUTPUT_DIR"]); out.mkdir(parents=True, exist_ok=True)
(out / "result.txt").write_text("seed=%d" % args.seed, encoding="utf-8")
"""


@pytest.fixture
def project_dir(tmp_path) -> Path:
    path = tmp_path / "proj"
    (path / "data").mkdir(parents=True)
    (path / "data" / "a.dat").write_text("1 2 3\n", encoding="utf-8")
    (path / "data" / "b.dat").write_text("4 5 6 7\n", encoding="utf-8")
    (path / "train.py").write_text(TRAIN_PY, encoding="utf-8")
    return path


def build_service(
    tmp_path,
    project_dir: Path,
    *,
    inputs: str = "",
    ttl: int = 30,
    data_paths: str = "",
) -> RcfService:
    home = make_home(
        tmp_path,
        settings=f"""\
[security]
allow_execute = true

[runs]
plan_ttl_minutes = {ttl}
""",
        nodes="""\
[nodes.laptop]
kind = "local"
""",
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}} --seconds {{seconds}}"
output_contract = "env"
script = "train.py"
outputs = "outputs"
inputs = [{inputs}]
paths = {{ "laptop" = "{toml_path(project_dir)}" }}
data_paths = {{{data_paths}}}
""",
        environments=f"""\
[environments.py]
kind = "venv"
path = {{ "laptop" = "{toml_path(project_dir / 'venv')}" }}
managed = true
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner(), actor="test")
    snap = ResourceSnapshot(
        node_id="laptop",
        observed_at=datetime.now(UTC),
        adapter="local",
        home_dir=str(Path.home()),
        cpu=CpuInfo(logical_cores=16, load_1m=0.5),
        memory=MemoryInfo(total_mb=64000, available_mb=32000),
        gpus=[],
        scheduler=SchedulerView(available=False),
        checks=[Check(name="host", status="ok", detail="fake")],
    )
    service.inspector.probe = lambda node: ProbeOutcome(snapshot=snap)  # type: ignore[assignment]
    service.inspector.invalidate()
    return service


def test_declared_inputs_are_resolved_and_recorded(tmp_path, project_dir):
    svc = build_service(tmp_path, project_dir, inputs='"data/*.dat"')
    try:
        plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        check = next(c for c in plan["checks"] if c["name"] == "project_inputs")
        assert check["status"] == "ok", check
        manifest = plan["inputs"]
        assert [f["path"] for f in manifest["files"]] == ["data/a.dat", "data/b.dat"]
        assert manifest["total_bytes"] == (
            (project_dir / "data" / "a.dat").stat().st_size
            + (project_dir / "data" / "b.dat").stat().st_size
        )
        assert len(manifest["digest"]) == 64
    finally:
        svc.close()


def test_missing_input_blocks_the_plan(tmp_path, project_dir):
    svc = build_service(tmp_path, project_dir, inputs='"data/*.dat", "still/*.npy"')
    try:
        plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        check = next(c for c in plan["checks"] if c["name"] == "project_inputs")
        assert check["status"] == "fail"
        assert "still/*.npy" in check["detail"]
        assert plan["inputs"] is None
        assert any(c["status"] == "fail" for c in plan["checks"])
    finally:
        svc.close()


def test_submitting_an_expired_plan_is_refused(tmp_path, project_dir):
    svc = build_service(tmp_path, project_dir, ttl=30)
    try:
        plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        stored = svc.store.get_plan(plan["plan_id"])
        assert stored is not None
        stored.created_at = datetime.now(UTC) - timedelta(minutes=31)
        svc.store.save_plan(stored)

        with pytest.raises(PlanExpired) as excinfo:
            svc.submit_run(plan["plan_id"], confirm=True)
        assert "31 minutes old" in str(excinfo.value)
        assert svc.store.get_run(plan["run_id"]) is None
    finally:
        svc.close()


def test_fresh_plan_is_submittable(tmp_path, project_dir):
    svc = build_service(tmp_path, project_dir, ttl=30)
    try:
        plan = svc.plan_run({"project_id": "demo", "params": {"seed": 2, "seconds": 0}})
        record = svc.submit_run(plan["plan_id"], confirm=True)
        assert record["run_id"] == plan["run_id"]
    finally:
        svc.close()


def test_environment_lock_lists_packages(tmp_path, project_dir, monkeypatch):
    svc = build_service(tmp_path, project_dir)
    try:
        calls: list[str] = []

        def fake_shell(env, node, command, *, timeout):
            calls.append(command)
            if "pip freeze" in command:
                return CommandResult(
                    argv=("pip", "freeze"), returncode=0, stdout="numpy==2.0.1\nscipy==1.14.0\n"
                )
            return CommandResult(argv=("bash", "-lc", command), returncode=1, stderr="n/a")

        monkeypatch.setattr(svc.environments, "_run_shell", fake_shell)
        payload = svc.lock_environment("py", "laptop")
        text = payload["lock"]
        assert "# environment: py" in text
        assert "numpy==2.0.1" in text
        assert "scipy==1.14.0" in text
        assert any("pip freeze" in c for c in calls)
    finally:
        svc.close()


def test_environment_lock_needs_a_path(tmp_path, project_dir):
    svc = build_service(tmp_path, project_dir)
    try:
        from rcf.core.models import NodeSpec

        svc.config.nodes["elsewhere"] = NodeSpec(id="elsewhere", kind="local")
        svc.environments._run_shell = lambda *a, **k: CommandResult(  # type: ignore[assignment]
            argv=("bash",), returncode=1
        )
        with pytest.raises(NotFound):
            svc.environments.lock(svc.config.environment("py"), svc.config.node("elsewhere"))
    finally:
        svc.close()


def test_data_inputs_resolve_under_the_data_root(tmp_path, project_dir):
    data = tmp_path / "data-root"
    (data / "init").mkdir(parents=True)
    (data / "init" / "conf.npy").write_bytes(b"" * 32)
    svc = build_service(
        tmp_path,
        project_dir,
        inputs='"data/*.dat", "data:init/*.npy"',
        data_paths=f'"laptop" = "{toml_path(data)}"',
    )
    try:
        plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        check = next(c for c in plan["checks"] if c["name"] == "project_inputs")
        assert check["status"] == "ok", check
        manifest = plan["inputs"]
        roots = {(f["root"], f["path"]) for f in manifest["files"]}
        assert ("project", "data/a.dat") in roots
        assert ("data", "init/conf.npy") in roots
    finally:
        svc.close()


def test_data_inputs_without_a_data_root_are_refused(tmp_path, project_dir):
    svc = build_service(tmp_path, project_dir, inputs='"data:init/*.npy"')
    try:
        plan = svc.plan_run({"project_id": "demo", "params": {"seed": 1, "seconds": 0}})
        check = next(c for c in plan["checks"] if c["name"] == "project_inputs")
        assert check["status"] == "fail"
        assert "data_paths" in check["detail"]
        assert plan["inputs"] is None
    finally:
        svc.close()
