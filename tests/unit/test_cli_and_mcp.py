"""CLI and MCP surfaces: same core, same data, structured errors."""

from __future__ import annotations

import asyncio
import json

import pytest
import typer.main
from conftest import make_home, toml_path
from typer.testing import CliRunner

from rcf import __version__
from rcf.cli.main import app
from rcf.mcp.server import TOOL_SPECS, build_server
from rcf.service import RcfService

PROJECT = """\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}}"
output_contract = "env"
script = "train.py"
paths = {{ "laptop" = "{path}" }}
"""


@pytest.fixture
def home(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "train.py").write_text("print('ok')\n", encoding="utf-8")
    return make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=PROJECT.format(path=toml_path(project)),
    )


def invoke(home, *args: str):
    return CliRunner().invoke(app, ["--home", str(home), *args])


def test_cli_version_flag_matches_the_command(home):
    flag = CliRunner().invoke(app, ["--version"])
    sub = CliRunner().invoke(app, ["version"])

    assert flag.exit_code == 0, flag.output
    assert flag.output.strip() == sub.output.strip() == f"rcf {__version__}"


def test_cli_node_list_json(home):
    result = invoke(home, "--json", "node", "list")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["id"] == "laptop"


def test_cli_doctor_reports_status(home):
    result = invoke(home, "--json", "doctor")
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert {c["name"] for c in payload["checks"]} >= {"python", "ssh", "nodes"}


def test_cli_unknown_node_exits_with_not_found(home):
    result = invoke(home, "node", "show", "ghost")
    assert result.exit_code == 4
    assert "unknown node" in result.output


def test_cli_missing_placeholder_exits_with_spec_error(home):
    result = invoke(home, "run", "plan", "--project", "demo")
    assert result.exit_code == 2
    assert "unknown placeholder" in result.output


def test_cli_run_plan_emits_json(home):
    result = invoke(home, "--json", "run", "plan", "--project", "demo", "--param", "seed=5")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["command"]["argv"][-2:] == ["--seed", "5"]
    assert plan["executor"] == "local"


def test_cli_run_submit_and_status(home):
    planned = invoke(home, "--json", "run", "plan", "--project", "demo", "--param", "seed=5")
    plan_id = json.loads(planned.output)["plan_id"]

    denied = invoke(home, "run", "submit", plan_id)
    assert denied.exit_code == 7  # approval required

    submitted = invoke(home, "--json", "run", "submit", plan_id, "--yes")
    assert submitted.exit_code == 0, submitted.output
    record = json.loads(submitted.output)
    assert record["state"] in {"pending", "running", "succeeded"}

    status = invoke(home, "--json", "run", "status", record["run_id"])
    assert json.loads(status.output)["run_id"] == record["run_id"]


def test_cli_rcf_json_env_var(home, monkeypatch):
    monkeypatch.setenv("RCF_JSON", "1")
    result = invoke(home, "node", "list")
    assert json.loads(result.output)[0]["id"] == "laptop"


def test_mcp_tool_specs_match_cli_and_service():
    group = typer.main.get_command(app)

    def resolve(path):
        node = group
        for name in path:
            assert name in node.commands, f"missing CLI command: {' '.join(path)}"
            node = node.commands[name]
        return node

    for spec in TOOL_SPECS:
        assert hasattr(RcfService, spec.service_method), spec.service_method
        assert resolve(spec.cli_path) is not None
        assert spec.name.startswith("compute_")
        assert spec.dotted.startswith("compute.")


def test_mcp_server_exposes_every_spec():
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {spec.name for spec in TOOL_SPECS}


def call_tool(server, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(server.call_tool(name, arguments or {}))
    return json.loads(result.content[0].text)


def test_mcp_list_nodes(home):
    server = build_server(home=str(home))
    payload = call_tool(server, "compute_list_nodes")
    assert payload["ok"] is True
    assert payload["data"][0]["id"] == "laptop"


def test_mcp_unknown_node_is_structured_error(home):
    server = build_server(home=str(home))
    payload = call_tool(server, "compute_inspect_node", {"node_id": "ghost"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "hint" in payload["error"]


def test_mcp_submit_requires_confirm(home):
    server = build_server(home=str(home))
    planned = call_tool(
        server, "compute_plan_run", {"project_id": "demo", "params": {"seed": 1}}
    )
    assert planned["ok"] is True
    plan_id = planned["data"]["plan_id"]

    refused = call_tool(server, "compute_submit_run", {"plan_id": plan_id})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "permission_denied"

    accepted = call_tool(server, "compute_submit_run", {"plan_id": plan_id, "confirm": True})
    assert accepted["ok"] is True
    assert accepted["data"]["state"] in {"pending", "running", "succeeded"}


def test_mcp_plan_is_pure(home):
    """Planning must not create runs or leases."""

    server = build_server(home=str(home))
    payload = call_tool(server, "compute_plan_run", {"project_id": "demo", "params": {"seed": 2}})
    assert payload["data"]["run_id"]
    with RcfService(read_service_home(home)) as svc:
        assert svc.list_runs() == []
        assert svc.store.active_leases() == {}


def read_service_home(home):
    from rcf.core.config import Config

    return Config.load(home)
