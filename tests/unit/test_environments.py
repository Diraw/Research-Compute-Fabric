"""Environment inspection and install planning (read-only by default)."""

from __future__ import annotations

import json

import pytest
from conftest import make_home

from rcf.core.config import Config
from rcf.core.errors import PermissionDenied
from rcf.core.models import EnvironmentSpec, NodeSpec
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.environments.manager import EnvironmentManager


def probe_payload(python="3.11.6", packages=None) -> CommandResult:
    return CommandResult(
        argv=(),
        returncode=0,
        stdout=json.dumps(
            {
                "python": python,
                "executable": "/opt/env/bin/python",
                "platform": "linux",
                "packages": packages if packages is not None else {"numpy": "1.26.4"},
            }
        ),
    )


def manager(tmp_path, *, settings=None, runner=None, managed_roots=None) -> EnvironmentManager:
    if managed_roots is not None and settings is None:
        roots = ", ".join(f'"{r}"' for r in managed_roots)
        settings = (
            "[environments]\n"
            f"managed_roots = [{roots}]\n"
        )
    home = make_home(tmp_path, settings=settings) if settings else make_home(tmp_path)
    config = Config.load(home)
    mgr = EnvironmentManager(config, runner or ScriptedRunner(), store=None)
    # These command-construction tests model a POSIX prefix, even on Windows.
    # Filesystem safety is exercised with real temp paths in path-guard tests.
    from rcf.environments.ownership import PrefixVerdict
    from rcf.environments.path_guard import PrefixPathInspection

    mgr._inspect_path = lambda node, path: PrefixPathInspection(  # type: ignore[method-assign]
        PrefixVerdict(True, "synthetic test prefix"), path=path,
        exists=False,
    )
    return mgr


ROOTS = ["/home/me/envs"]


LOCAL = NodeSpec(id="laptop", kind="local")
VENV = EnvironmentSpec(
    id="sci",
    kind="venv",
    python="python",
    path={"laptop": "/home/me/envs/sci"},
    packages=[{"name": "numpy", "version": ">=1.26"}],
)


def test_compatible_when_requirements_are_met(tmp_path):
    mgr = manager(tmp_path, runner=ScriptedRunner([probe_payload()]))
    view = mgr.inspect(VENV, LOCAL)
    assert view.state == "compatible"
    assert view.python_version == "3.11.6"
    assert view.fingerprint


def test_missing_package_keeps_state_installed(tmp_path):
    mgr = manager(tmp_path, runner=ScriptedRunner([probe_payload(packages={"numpy": None})]))
    view = mgr.inspect(VENV, LOCAL)
    assert view.state == "installed"
    assert view.missing_packages == ["numpy"]


def test_version_mismatch_is_reported(tmp_path):
    mgr = manager(tmp_path, runner=ScriptedRunner([probe_payload(packages={"numpy": "1.20.0"})]))
    view = mgr.inspect(VENV, LOCAL)
    assert view.state == "installed"
    assert any(c.name == "pkg:numpy" and c.status == "fail" for c in view.checks)


def test_python_version_requirement(tmp_path):
    env = VENV.model_copy(update={"python_version": ">=3.12"})
    mgr = manager(tmp_path, runner=ScriptedRunner([probe_payload()]))
    view = mgr.inspect(env, LOCAL)
    assert view.state == "installed"
    assert any(c.name == "python_version" and c.status == "fail" for c in view.checks)


def test_missing_interpreter(tmp_path):
    failure = CommandResult(argv=(), returncode=127, stderr="No such file or directory")
    mgr = manager(tmp_path, runner=ScriptedRunner([failure]))
    view = mgr.inspect(VENV, LOCAL)
    assert view.state == "missing"
    assert view.checks[0].status == "fail"


def test_verify_command_marks_environment_verified(tmp_path):
    env = VENV.model_copy(update={"verify_command": "python -c 'import numpy'"})
    runner = ScriptedRunner([probe_payload(), CommandResult(argv=(), returncode=0, stdout="ok")])
    mgr = manager(tmp_path, runner=runner)
    view = mgr.inspect(env, LOCAL, verify=True, confirmed=True)
    assert view.state == "verified"
    assert view.verified_at is not None


def test_plan_for_missing_venv_creates_it(tmp_path):
    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    runner = ScriptedRunner([failure, CommandResult(argv=(), returncode=0, stdout="yes\n")])
    mgr = manager(tmp_path, runner=runner, managed_roots=ROOTS)
    plan = mgr.plan(VENV, LOCAL)
    assert plan.action == "create"
    assert plan.requires_authorization
    commands = [" ".join(step.command) for step in plan.steps]
    assert any("venv" in c for c in commands)
    assert any("install" in c for c in commands)


def test_plan_never_modifies_system_environments(tmp_path):
    env = EnvironmentSpec(id="sys", kind="system", python="python3")
    failure = CommandResult(argv=(), returncode=1, stderr="nope")
    mgr = manager(tmp_path, runner=ScriptedRunner([failure]))
    plan = mgr.plan(env, LOCAL)
    assert plan.action == "blocked"
    assert plan.steps == []
    assert any("system" in blocker for blocker in plan.blockers)


def test_apply_requires_confirmation(tmp_path):
    settings = (
        "[security]\n"
        "allow_execute = true\n"
        "require_confirmation = true\n"
        "\n[environments]\n"
        'managed_roots = ["/home/me/envs"]\n'
    )
    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    runner = ScriptedRunner([failure, CommandResult(argv=(), returncode=0, stdout="yes\n")])
    mgr = manager(tmp_path, settings=settings, runner=runner)
    plan = mgr.plan(VENV, LOCAL)
    with pytest.raises(PermissionDenied, match="confirmation"):
        mgr.apply(plan, VENV, LOCAL, confirmed=False)


def test_no_managed_roots_means_no_mutation(tmp_path):
    """Location, not a boolean, decides: no declared root -> nothing is writable."""

    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    runner = ScriptedRunner([failure, CommandResult(argv=(), returncode=0, stdout="yes\n")])
    mgr = manager(tmp_path, runner=runner)
    plan = mgr.plan(VENV, LOCAL)
    assert plan.action == "blocked"
    assert any("managed_roots" in blocker for blocker in plan.blockers)


def test_apply_runs_steps_inside_a_managed_root(tmp_path):
    settings = """\
[security]
allow_execute = true
require_confirmation = true

[environments]
managed_roots = ["/home/me/envs"]
"""
    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    runner = ScriptedRunner(
        [
            failure,  # probe: environment missing
            CommandResult(argv=(), returncode=0, stdout="yes\n"),  # uv present
            CommandResult(argv=(), returncode=0, stdout="created"),  # uv venv
            CommandResult(argv=(), returncode=0, stdout="installed"),  # uv pip install
        ]
    )
    mgr = manager(tmp_path, settings=settings, runner=runner)
    plan = mgr.plan(VENV, LOCAL)
    results = mgr.apply(plan, VENV, LOCAL, confirmed=True)
    assert [r["returncode"] for r in results] == [0, 0]
    assert "uv venv" in " ".join(results[0]["command"])


def test_environment_without_path_for_node_is_missing(tmp_path):
    env = EnvironmentSpec(id="x", kind="venv", path={"other": "/e"})
    mgr = manager(tmp_path, runner=ScriptedRunner())
    view = mgr.inspect(env, LOCAL)
    assert view.state == "missing"
    assert "no path configured" in view.checks[0].detail


def test_interpreter_path_shape_matches_the_platform():
    import os

    env = EnvironmentSpec(id="v", kind="venv", python="python", path={"laptop": "C:/envs/v"})
    interpreter = EnvironmentManager.interpreter(env, LOCAL)
    assert interpreter == (
        ["C:/envs/v/Scripts/python.exe"] if os.name == "nt" else ["C:/envs/v/bin/python"]
    )


def test_interpreter_for_system_environment_is_the_bare_name():
    env = EnvironmentSpec(id="s", kind="system", python="python3")
    assert EnvironmentManager.interpreter(env, LOCAL) == ["python3"]


def test_fingerprint_covers_the_whole_installed_set(tmp_path):
    """A transitive upgrade must change the fingerprint, not just a declared bump."""

    base = {
        "python": "3.11.6",
        "executable": "/opt/env/bin/python",
        "platform": "linux",
        "packages": {"numpy": "1.26.4"},
        "dist_count": 200,
        "env_digest": "aaaaaaaaaaaaaaaa",
    }
    other = dict(base, env_digest="bbbbbbbbbbbbbbbb")

    def view_for(payload):
        runner = ScriptedRunner(
            [CommandResult(argv=(), returncode=0, stdout=json.dumps(payload))]
        )
        mgr = manager(tmp_path, runner=runner)
        return mgr.inspect(VENV, LOCAL)

    first, second = view_for(base), view_for(other)
    assert first.state == "compatible" and second.state == "compatible"
    assert first.env_digest != second.env_digest
    assert first.fingerprint != second.fingerprint
    assert first.dist_count == 200


def test_conda_preamble_activates_by_prefix_not_by_id():
    """Registry and conda environment names may differ; activate the prefix."""

    from rcf.core.models import NodeSpec

    node = NodeSpec(id="ws", kind="ssh", ssh_alias="ws", work_root="/home/researcher/rcf")
    env = EnvironmentSpec(
        id="science",
        kind="conda",
        conda_root="/home/researcher/miniforge3",
        path={"ws": "/home/researcher/miniforge3/envs/runtime"},
    )
    lines = EnvironmentManager.preamble(env, node)
    assert "conda activate /home/researcher/miniforge3/envs/runtime 2>/dev/null || true" in lines
    assert not any("conda activate science" in line for line in lines)


def test_conda_preamble_falls_back_to_id_without_a_path():
    env = EnvironmentSpec(id="sci", kind="conda", conda_root="/opt/conda")
    lines = EnvironmentManager.preamble(env)
    assert "conda activate sci 2>/dev/null || true" in lines


def test_venv_can_sit_on_top_of_modules():
    """HPC pattern: load the toolchain with modules, install packages in a venv."""

    from rcf.core.models import NodeSpec

    node = NodeSpec(
        id="hpc",
        kind="slurm",
        ssh_alias="hpc",
        work_root="/public/home/me/rcf",
        slurm={"default_partition": "debug"},
    )
    env = EnvironmentSpec(
        id="py312",
        kind="venv",
        python="python3.12",
        path={"hpc": "/public/home/me/envs/py312"},
        modules=["compiler/python/3.12.6"],
        packages=[{"name": "numpy", "version": ">=1.26"}],
    )
    assert EnvironmentManager.interpreter(env, node) == ["/public/home/me/envs/py312/bin/python3.12"]
    lines = EnvironmentManager.preamble(env, node)
    assert any("module load compiler/python/3.12.6" in line for line in lines)
    assert any("modules.sh" in line for line in lines)


def test_prefix_outside_managed_roots_is_refused(tmp_path):
    from rcf.environments.ownership import check_prefix

    verdict = check_prefix(
        path="/data/other/env",
        home="/home/me",
        managed_roots=["/home/me/envs"],
        exists=False,
        known_to_rcf=False,
        declared_managed=False,
    )
    assert verdict.blocked
    assert "outside the managed roots" in verdict.reason


def test_shared_system_path_is_refused_even_if_declared(tmp_path):
    from rcf.environments.ownership import check_prefix

    verdict = check_prefix(
        path="/usr/lib/python3",
        home="/home/me",
        managed_roots=["/usr"],
        exists=False,
        known_to_rcf=False,
        declared_managed=True,
    )
    assert verdict.blocked
    assert "shared/system path" in verdict.reason


def test_existing_hand_made_env_needs_an_explicit_handover():
    from rcf.environments.ownership import check_prefix

    base = dict(path="/home/me/envs/runtime", home="/home/me", managed_roots=["/home/me/envs"], exists=True)
    assert check_prefix(**base, known_to_rcf=False, declared_managed=False).blocked
    assert not check_prefix(**base, known_to_rcf=True, declared_managed=False).blocked
    assert not check_prefix(**base, known_to_rcf=False, declared_managed=True).blocked


def test_venv_can_be_created_from_a_non_path_interpreter(tmp_path):
    """miniforge in your home is not on PATH; the venv still gets its own python."""

    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    runner = ScriptedRunner([failure, CommandResult(argv=(), returncode=0, stdout="no\n")])
    mgr = manager(tmp_path, runner=runner, managed_roots=ROOTS)
    env = VENV.model_copy(update={"base_python": "/public/home/me/miniforge3/bin/python"})
    plan = mgr.plan(env, LOCAL)
    create = next(step for step in plan.steps if "venv" in " ".join(step.command))
    assert create.command[0] == "/public/home/me/miniforge3/bin/python"
    assert "/home/me/envs/sci" in create.command


CONDA = EnvironmentSpec(
    id="sci",
    kind="conda",
    conda_root="/opt/miniforge3",
    python="python3",
    conda_python="3.12",
    path={"laptop": "/home/me/envs/sci"},
    packages=[{"name": "numpy", "version": ">=1.26"}],
)


def test_conda_environment_is_created_with_conda(tmp_path):
    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    mgr = manager(tmp_path, runner=ScriptedRunner([failure]), managed_roots=ROOTS)
    plan = mgr.plan(CONDA, LOCAL)
    assert plan.action == "create"
    command = plan.steps[0].command
    assert command[:5] == [
        "/opt/miniforge3/bin/conda",
        "create",
        "-y",
        "-p",
        "/home/me/envs/sci",
    ]
    assert "python=3.12" in command
    assert "numpy>=1.26" in command


def test_conda_creation_needs_an_exact_python_version(tmp_path):
    failure = CommandResult(argv=(), returncode=127, stderr="missing")
    mgr = manager(tmp_path, runner=ScriptedRunner([failure]), managed_roots=ROOTS)
    env = CONDA.model_copy(update={"conda_python": None})
    plan = mgr.plan(env, LOCAL)
    assert plan.action == "blocked"
    assert any("conda_python" in blocker for blocker in plan.blockers)


def test_missing_conda_package_is_installed_with_conda(tmp_path):
    payload = probe_payload(packages={"numpy": None})
    mgr = manager(tmp_path, runner=ScriptedRunner([payload]), managed_roots=ROOTS)
    plan = mgr.plan(CONDA, LOCAL)
    assert plan.action == "install"
    command = plan.steps[0].command
    assert command[:4] == ["/opt/miniforge3/bin/conda", "install", "-y", "-p"]
    assert "numpy>=1.26" in command
