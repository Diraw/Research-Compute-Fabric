"""Submission evidence checks never contact real SSH or invoke a scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import Conflict, HostUnreachable, RcfError
from rcf.core.models import (
    Check,
    EnvironmentView,
    InputManifest,
    PlannedCommand,
    RunPlan,
    RunRequest,
)
from rcf.core.nodeops import NodeOps
from rcf.projects.service import ProjectService
from rcf.runs.planner import RunPlanner
from rcf.runs.preflight import validate_plan


@pytest.fixture
def evidence(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "sample.dat").write_bytes(b"abc")
    data = tmp_path / "data"
    data.mkdir()
    (data / "sample.bin").write_bytes(b"1234")
    config = Config.load(
        make_home(
            tmp_path,
            nodes='[nodes.local]\nkind = "local"\n',
            projects=(
                '[projects.demo]\n'
                f'paths = {{ local = "{toml_path(checkout)}" }}\n'
                f'data_paths = {{ local = "{toml_path(data)}" }}\n'
                'inputs = ["*.dat", "data:*.bin"]\n'
            ),
            environments='[environments.py]\nkind = "system"\n',
        )
    )
    # Local input expansion is real; every command/probe is otherwise a test double.
    runner = Mock()
    runner.run.side_effect = AssertionError("unexpected external command")
    ops = NodeOps(runner)
    ops.git_state = Mock(
        return_value={"rcf_repo": "git", "rcf_commit": "a" * 40, "rcf_dirty": "no"}
    )
    environments = Mock()
    environments.inspect.return_value = EnvironmentView(
        env_id="py", node_id="local", kind="system", state="compatible", fingerprint="env-1"
    )
    projects = ProjectService(config, runner, node_ops=ops)
    planner = RunPlanner(config, Mock(), Mock(), environments, projects, node_ops=ops)
    _, manifest = planner._inputs_check(config.project("demo"), config.node("local"), str(checkout))
    planned_path = config.project("demo").path_for("local")
    plan = RunPlan(
        plan_id="plan_test",
        run_id="run_test",
        created_at=datetime.now(UTC),
        request=RunRequest(project_id="demo"),
        node_id="local",
        node_kind="local",
        executor="local",
        command=PlannedCommand(argv=["python", "train.py"], workdir=planned_path),
        workdir=planned_path,
        project_path=planned_path,
        git_commit="a" * 40,
        git_dirty=False,
        env_id="py",
        env_fingerprint="env-1",
        inputs=manifest,
    )
    return config, planner, plan


def assert_replan(error: RcfError, evidence: str) -> None:
    assert error.hint and "plan" in error.hint.lower()
    assert error.detail["plan_id"] == "plan_test"
    assert error.detail["node_id"] == "local"
    assert error.detail["evidence"] == evidence


def test_unchanged_evidence_passes_without_mutation(evidence):
    config, planner, plan = evidence
    before = plan.model_dump()
    assert validate_plan(config, planner, plan) is None
    assert plan.model_dump() == before
    planner.environments.inspect.assert_called_once_with(
        config.environment("py"), config.node("local"), verify=False
    )
    planner.ops.git_state.assert_called_once_with(config.node("local"), plan.project_path)
    planner.projects.runner.run.assert_not_called()


@pytest.mark.parametrize(
    ("commit", "dirty"),
    [("b" * 40, "no"), ("a" * 40, "yes"), (None, "no"), ("a" * 40, None)],
)
def test_git_drift_or_unavailable_evidence_is_rejected(evidence, commit, dirty):
    config, planner, plan = evidence
    planner.ops.git_state.return_value = {
        "rcf_repo": "git", "rcf_commit": commit, "rcf_dirty": dirty
    }
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "git")


def test_dirty_to_clean_is_also_drift(evidence):
    config, planner, plan = evidence
    plan.git_dirty = True
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "git")


def test_still_dirty_is_only_boolean_evidence_not_content_hash(evidence):
    config, planner, plan = evidence
    plan.git_dirty = True
    planner.ops.git_state.return_value["rcf_dirty"] = "yes"
    assert validate_plan(config, planner, plan) is None


def test_unknown_original_dirty_does_not_invent_a_baseline(evidence):
    config, planner, plan = evidence
    plan.git_dirty = None
    planner.ops.git_state.return_value["rcf_dirty"] = "yes"
    assert validate_plan(config, planner, plan) is None


def test_no_git_commit_means_no_new_git_probe(evidence):
    config, planner, plan = evidence
    plan.git_commit = None
    planner.ops.git_state.side_effect = AssertionError("git was not recorded")
    assert validate_plan(config, planner, plan) is None
    planner.ops.git_state.assert_not_called()


@pytest.mark.parametrize(("env_id", "fingerprint"), [(None, "env-1"), ("py", None), (None, None)])
def test_incomplete_environment_evidence_is_not_probed(evidence, env_id, fingerprint):
    config, planner, plan = evidence
    plan.env_id, plan.env_fingerprint = env_id, fingerprint
    assert validate_plan(config, planner, plan) is None
    planner.environments.inspect.assert_not_called()


@pytest.mark.parametrize(("fingerprint", "state"), [("env-2", "compatible"), (None, "missing"), ("env-1", "unknown")])
def test_changed_or_unusable_environment_is_rejected(evidence, fingerprint, state):
    config, planner, plan = evidence
    planner.environments.inspect.return_value.fingerprint = fingerprint
    planner.environments.inspect.return_value.state = state
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "environment")


@pytest.mark.parametrize("probe", ["git", "environment", "inputs"])
@pytest.mark.parametrize("failure", [HostUnreachable("offline"), OSError("unreadable")])
def test_probe_errors_are_structured_with_replan_hint(evidence, probe, failure):
    config, planner, plan = evidence
    target = {
        "git": planner.ops.git_state,
        "environment": planner.environments.inspect,
        "inputs": None,
    }[probe]
    if probe == "inputs":
        planner.ops.resolve_inputs = Mock(side_effect=failure)
    else:
        target.side_effect = failure
    with pytest.raises(RcfError) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, probe)


@pytest.mark.parametrize("mutation", ["resize", "add", "delete", "data_resize"])
def test_declared_input_listing_drift_is_rejected(evidence, mutation):
    config, planner, plan = evidence
    checkout = config.project("demo").path_for("local")
    if mutation == "resize":
        (Path(checkout) / "sample.dat").write_bytes(b"longer")
    elif mutation == "add":
        (Path(checkout) / "extra.dat").write_bytes(b"x")
    elif mutation == "delete":
        (Path(checkout) / "sample.dat").unlink()
    else:
        (Path(config.project("demo").data_path_for("local")) / "sample.bin").write_bytes(b"longer")
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "inputs")


def test_same_size_content_change_is_outside_listing_evidence(evidence):
    config, planner, plan = evidence
    (Path(plan.project_path) / "sample.dat").write_bytes(b"xyz")
    assert validate_plan(config, planner, plan) is None


def test_removed_input_declarations_cannot_skip_recorded_manifest(evidence):
    config, planner, plan = evidence
    config.project("demo").inputs = []
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "inputs")


def test_no_manifest_does_not_add_input_probe(evidence):
    config, planner, plan = evidence
    plan.inputs = None
    planner._inputs_check = Mock(side_effect=AssertionError("no input evidence"))
    assert validate_plan(config, planner, plan) is None
    planner._inputs_check.assert_not_called()


@pytest.mark.parametrize("probe_result", [(None, None), (Check(name="project_inputs", status="unknown"), InputManifest.build())])
def test_inputs_require_successful_check_and_manifest(evidence, probe_result):
    config, planner, plan = evidence
    planner._inputs_check = Mock(return_value=probe_result)
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "inputs")


def test_configured_checkout_cannot_silently_change_probe_target(evidence):
    config, planner, plan = evidence
    config.project("demo").paths["local"] = "/different/checkout"
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "project_path")


def test_no_recorded_evidence_performs_no_probes(evidence):
    config, planner, plan = evidence
    plan.git_commit = None
    plan.env_fingerprint = None
    plan.inputs = None
    planner.projects.view = Mock(side_effect=AssertionError("no git evidence"))
    planner._inputs_check = Mock(side_effect=AssertionError("no manifest"))
    assert validate_plan(config, planner, plan) is None
    planner.projects.view.assert_not_called()
    planner.environments.inspect.assert_not_called()
    planner._inputs_check.assert_not_called()


def test_missing_checkout_is_rejected(evidence):
    config, planner, plan = evidence
    planner.ops.git_state.return_value = {"rcf_repo": "missing"}
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "git")


def test_failed_input_check_is_not_overridden_by_matching_digest(evidence):
    config, planner, plan = evidence
    planner._inputs_check = Mock(
        return_value=(Check(name="project_inputs", status="fail", detail="probe failed"), plan.inputs)
    )
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "inputs")
    assert exc.value.detail["reason"] == "probe failed"


def test_service_conflict_gets_submission_context(evidence):
    config, planner, plan = evidence
    planner.environments.inspect.side_effect = Conflict("environment inspection unavailable")
    with pytest.raises(Conflict) as exc:
        validate_plan(config, planner, plan)
    assert_replan(exc.value, "environment")
    assert exc.value.detail["cause"]["code"] == "conflict"


@pytest.mark.parametrize("registry", ["nodes", "projects", "environments"])
def test_missing_registry_entry_requires_new_plan(evidence, registry):
    config, planner, plan = evidence
    getattr(config, registry).clear()
    with pytest.raises(RcfError) as exc:
        validate_plan(config, planner, plan)
    assert exc.value.hint and "plan" in exc.value.hint.lower()
    assert exc.value.detail["plan_id"] == plan.plan_id
