"""Sweeps: plan the whole grid first, then submit it, and never half-run blind.

The value of a sweep over a shell loop is the preflight: one unschedulable point
must stop the request before the other twenty are queued.  These tests pin that,
plus the bookkeeping that lets one sweep be listed afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import HELLO_PY, make_home, toml_path
from typer.testing import CliRunner

from rcf.cli.main import app
from rcf.core.config import Config
from rcf.core.errors import SpecError
from rcf.runs.sweep import expand_grid
from rcf.service import RcfService


@pytest.fixture
def service(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    home = make_home(
        tmp_path / "svc",
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.hello]
entrypoint = "{{python}} hello.py --seed {{seed}}"
output_contract = "env"
script = "hello.py"
paths = {{ "laptop" = "{toml_path(project)}" }}
""",
    )
    svc = RcfService(Config.load(home), actor="test")
    yield svc
    svc.close()


def request(**params):
    return {"project_id": "hello", "node_id": "laptop", "params": params}


# -- expansion -------------------------------------------------------------- #


def test_the_grid_is_a_cartesian_product_with_the_last_parameter_fastest():
    combos = expand_grid({"seed": [1, 2], "mode": ["fast", "slow"]})

    assert combos == [
        {"seed": 1, "mode": "fast"},
        {"seed": 1, "mode": "slow"},
        {"seed": 2, "mode": "fast"},
        {"seed": 2, "mode": "slow"},
    ]


def test_a_sweep_needs_something_to_vary():
    with pytest.raises(SpecError) as caught:
        expand_grid({})

    assert caught.value.code == "spec_invalid"
    assert "--grid" in caught.value.hint


def test_an_empty_value_list_names_the_offending_parameter():
    with pytest.raises(SpecError) as caught:
        expand_grid({"seed": []})

    assert caught.value.detail["param"] == "seed"


def test_a_grid_above_the_limit_is_refused_with_the_count():
    with pytest.raises(SpecError) as caught:
        expand_grid({"seed": list(range(50)), "mode": ["a", "b", "c", "d", "e"]})

    assert caught.value.detail["combinations"] == 250
    assert caught.value.detail["limit"] == 200


# -- planning --------------------------------------------------------------- #


def test_planning_a_sweep_creates_one_plan_per_combination_and_no_runs(service):
    plan = service.plan_sweep(request(seed=0), {"seed": [1, 2, 3]}, max_runs=10)

    assert plan["combinations"] == 3
    assert plan["blocked"] == []
    assert len({entry["plan_id"] for entry in plan["entries"]}) == 3
    assert [entry["params"]["seed"] for entry in plan["entries"]] == [1, 2, 3]
    # Planning has no side effects beyond plan records.
    assert service.list_runs() == []


def test_fixed_parameters_survive_every_combination(service):
    plan = service.plan_sweep(request(seed=0, tag="base"), {"seed": [7, 8]}, max_runs=10)

    assert [entry["params"] for entry in plan["entries"]] == [
        {"seed": 7, "tag": "base"},
        {"seed": 8, "tag": "base"},
    ]


def test_one_impossible_combination_blocks_the_whole_sweep(service):
    request_with_gpu = {
        "project_id": "hello",
        "node_id": "laptop",
        "params": {"seed": 0},
        # A local node has no GPU capacity, so every combination fails the check.
        "requirements": {"gpus": 4},
    }

    plan = service.plan_sweep(request_with_gpu, {"seed": [1, 2]}, max_runs=10)

    assert len(plan["blocked"]) == 2
    assert all(entry["blocked_by"] for entry in plan["blocked"])


# -- submitting ------------------------------------------------------------- #


def test_submitting_without_confirmation_writes_nothing(service):
    plan = service.plan_sweep(request(seed=0), {"seed": [1, 2]}, max_runs=10)

    result = service.submit_sweep(
        plan["sweep_id"], [entry["plan_id"] for entry in plan["entries"]], confirm=False
    )

    assert result["submitted"] == []
    assert len(result["failed"]) == 2
    assert all("approval" in row["error"] or "confirm" in row["error"].lower() for row in result["failed"])
    assert service.list_runs() == []


def test_submitting_records_the_sweep_on_every_run(service):
    plan = service.plan_sweep(request(seed=0), {"seed": [3, 4]}, max_runs=10)

    result = service.submit_sweep(
        plan["sweep_id"], [entry["plan_id"] for entry in plan["entries"]], confirm=True
    )

    assert result["failed"] == []
    assert len(result["submitted"]) == 2

    rows = service.list_runs(sweep_id=plan["sweep_id"], limit=10)
    assert sorted(row["params"]["seed"] for row in rows) == [3, 4]
    assert all(row["sweep_id"] == plan["sweep_id"] for row in rows)
    assert service.list_runs(sweep_id="sweep_other") == []


# -- CLI -------------------------------------------------------------------- #


def invoke(home: Path, *args: str):
    return CliRunner().invoke(app, ["--home", str(home), *args])


def test_cli_sweep_plans_without_running_anything(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    home = make_home(
        tmp_path / "cli",
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.hello]
entrypoint = "{{python}} hello.py --seed {{seed}}"
output_contract = "env"
script = "hello.py"
paths = {{ "laptop" = "{toml_path(project)}" }}
""",
    )

    result = invoke(home, "run", "sweep", "-p", "hello", "--grid", "seed=1,2")

    assert result.exit_code == 0, result.output
    assert "2 combination(s)" in result.output
    assert "--yes" in result.output
    listed = invoke(home, "--json", "run", "list")
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == []
