"""Full local workflow through the CLI, with no simulated infrastructure.

This is the "one command works end to end" test: init a home, register a local
project, plan, submit, poll, read logs and results — all through the CLI an
Agent would call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_home, toml_path
from typer.testing import CliRunner

from rcf.cli.main import app

pytestmark = pytest.mark.e2e

SCRIPT = """import os
import pathlib

out = pathlib.Path(os.environ["RCF_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "answer.txt").write_text("42", encoding="utf-8")
print("computed")
"""


@pytest.fixture
def home(tmp_path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "compute.py").write_text(SCRIPT, encoding="utf-8")
    home = make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\nlabels = { site = "home" }\n',
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}}"
output_contract = "env"
script = "compute.py"
outputs = "outputs"
paths = {{ "laptop" = "{toml_path(project)}" }}
""",
    )
    return home, project


def cli(home: Path, *args: str, json_mode: bool = True):
    prefix = ["--home", str(home)]
    if json_mode:
        prefix.append("--json")
    return CliRunner().invoke(app, [*prefix, *args])


def test_local_workflow_end_to_end(home):
    home_dir, _ = home

    doctor = cli(home_dir, "doctor")
    assert doctor.exit_code == 0
    assert json.loads(doctor.output)["ok"] is True

    plan = json.loads(cli(home_dir, "run", "plan", "--project", "demo").output)
    assert plan["executor"] == "local"

    submitted = cli(home_dir, "run", "submit", plan["plan_id"], "--yes")
    assert submitted.exit_code == 0, submitted.output
    run_id = json.loads(submitted.output)["run_id"]

    status = json.loads(cli(home_dir, "run", "status", run_id).output)
    for _ in range(50):
        if status["state"] in {"succeeded", "failed"}:
            break
        status = json.loads(cli(home_dir, "run", "status", run_id).output)
    assert status["state"] == "succeeded", status
    assert status["exit_code"] == 0

    logs = json.loads(cli(home_dir, "run", "logs", run_id).output)
    assert "computed" in logs["log"]

    results = json.loads(cli(home_dir, "run", "results", run_id).output)
    result_file = Path(results["result_dir"]) / "answer.txt"
    assert result_file.read_text(encoding="utf-8") == "42"

    listed = json.loads(cli(home_dir, "run", "list").output)
    assert [row["run_id"] for row in listed] == [run_id]

    audit = json.loads(cli(home_dir, "db", "audit").output)
    assert any(row["operation"] == "run.submit" for row in audit)
