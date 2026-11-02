"""MPI launches: `--tasks`, the {launcher} placeholder, and the srun trap.

The ``srun`` path is exercised directly rather than through a full plan: a Slurm
node in these tests has no real SSH behind it, and the point is which launcher
gets chosen, not how the plan reaches the host.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.models import (
    Check,
    CpuInfo,
    EnvironmentKind,
    EnvironmentSpec,
    MemoryInfo,
    NodeKind,
    NodeSpec,
    ResourceRequirements,
    ResourceSnapshot,
    SchedulerView,
    SlurmSpec,
)
from rcf.core.runner import SubprocessRunner
from rcf.resources.probes import ProbeOutcome
from rcf.service import RcfService

SCRIPT = 'print("ok")\n'


def build(tmp_path, *, environments: str = "") -> RcfService:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "run.py").write_text(SCRIPT, encoding="utf-8")
    home = make_home(
        tmp_path,
        nodes="""\
[nodes.laptop]
kind = "local"
""",
        projects=f"""\
[projects.mpi]
entrypoint = "{{launcher}} {{python}} run.py --out {{output_dir}}"
script = "run.py"
paths = {{ "laptop" = "{toml_path(project)}" }}
""",
        environments=environments,
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner(), actor="test")
    snap = ResourceSnapshot(
        node_id="laptop",
        observed_at=datetime.now(UTC),
        adapter="local",
        home_dir="/home/user",
        cpu=CpuInfo(logical_cores=32, load_1m=0.1),
        memory=MemoryInfo(total_mb=128000, available_mb=64000),
        gpus=[],
        scheduler=SchedulerView(available=False),
        checks=[Check(name="host", status="ok", detail="fake")],
    )
    service.inspector.probe = lambda node: ProbeOutcome(snapshot=snap)  # type: ignore[assignment]
    service.inspector.invalidate()
    return service


def slurm_node() -> NodeSpec:
    return NodeSpec(
        id="cluster",
        kind=NodeKind.SLURM,
        ssh_alias="cluster",
        work_root="~/rcf",
        slurm=SlurmSpec(default_partition="debug"),
    )


def conda_env(launcher: str | None = None) -> EnvironmentSpec:
    return EnvironmentSpec(
        id="sci",
        kind=EnvironmentKind.CONDA,
        path={"cluster": "/home/user/envs/sci"},
        mpi_launcher=launcher,
    )


def test_single_rank_adds_no_launcher(tmp_path):
    svc = build(tmp_path)
    try:
        plan = svc.plan_run({"project_id": "mpi", "requirements": {"cpus": 4}})
        argv = plan["command"]["argv"]
        assert "run.py" in argv
        assert "" not in argv
        assert "srun" not in argv and "mpirun" not in argv and "mpiexec" not in argv
    finally:
        svc.close()


def test_more_ranks_than_cores_is_blocked(tmp_path):
    svc = build(tmp_path)
    try:
        plan = svc.plan_run({"project_id": "mpi", "requirements": {"cpus": 2, "tasks": 4}})
        blockers = {c["name"]: c["detail"] for c in plan["checks"] if c["status"] == "fail"}
        assert "tasks" in blockers
        assert "cannot fit" in blockers["tasks"]
    finally:
        svc.close()


def test_explicit_launcher_from_the_environment_wins(tmp_path):
    svc = build(tmp_path)
    try:
        planner = svc.runs.planner
        planner._env_has_command = lambda *a, **k: True  # type: ignore[assignment]
        token = planner._launcher_token(
            slurm_node(),
            ResourceRequirements(tasks=8),
            conda_env("mpiexec -n {tasks}"),
        )
        assert token == "mpiexec -n 8"
    finally:
        svc.close()


def test_slurm_uses_mpiexec_when_the_environment_ships_one(tmp_path):
    svc = build(tmp_path)
    try:
        planner = svc.runs.planner
        planner._env_has_command = lambda *a, **k: True  # type: ignore[assignment]
        token = planner._launcher_token(slurm_node(), ResourceRequirements(tasks=4), conda_env())
        assert token == "mpiexec -n 4"
    finally:
        svc.close()


def test_slurm_falls_back_to_srun_without_mpiexec(tmp_path):
    svc = build(tmp_path)
    try:
        planner = svc.runs.planner
        planner._env_has_command = lambda *a, **k: False  # type: ignore[assignment]
        token = planner._launcher_token(slurm_node(), ResourceRequirements(tasks=4), conda_env())
        # The conda MPICH on this cluster is built without PMI, so srun cannot
        # wire the ranks together -- it is only the fallback when nothing else
        # is available.
        assert token == "srun --exact -n 4 -c 1"
    finally:
        svc.close()
