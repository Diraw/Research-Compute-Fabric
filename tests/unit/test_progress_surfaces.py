"""Offline progress adapters: mocked services only, never submit/cancel a real run."""

from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import Mock, call

import pytest
import typer.main
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from rcf.cli.main import app
from rcf.core.errors import NotFound
from rcf.mcp.server import TOOL_SPECS, ToolRuntime, build_server

cli_module = importlib.import_module("rcf.cli.main")


@pytest.fixture
def service(monkeypatch):
    fake = Mock()
    fake.get_run_progress.return_value = {"run_id": "run-1", "revision": "rev-1"}
    fake.wait_run.return_value = {"run_id": "run-1", "revision": "rev-2", "timed_out": True}
    fake.plan_run.return_value = {"plan_id": "plan-1"}
    monkeypatch.setattr(cli_module, "_service", lambda state: fake)
    monkeypatch.setattr(ToolRuntime, "service", property(lambda self: fake))
    return fake


def cli(*args, as_json=True):
    return CliRunner().invoke(app, [*(["--json"] if as_json else []), *args])


def mcp_call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["progress", "run-1"], call.get_run_progress("run-1", refresh_state=True)),
        (["wait", "run-1"], call.wait_run(
            "run-1", timeout_seconds=30, after_revision=None, until="change")),
        *[
            (["wait", "run-1", "--timeout", "60", "--after", "rev-1", "--until", until],
             call.wait_run("run-1", timeout_seconds=60, after_revision="rev-1", until=until))
            for until in ("change", "terminal", "phase", "alert")
        ],
    ],
)
def test_cli_progress_wait_forward_only(service, args, expected):
    result = cli("run", *args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["run_id"] == "run-1"
    assert service.mock_calls == [expected]


@pytest.mark.parametrize("until", ["change", "terminal", "phase", "alert"])
def test_mcp_progress_wait_forward_only(service, until):
    server = build_server()
    assert mcp_call(server, "compute_get_run_progress", {"run_id": "run-1"}) == {
        "ok": True, "data": service.get_run_progress.return_value,
    }
    assert mcp_call(server, "compute_wait_run", {
        "run_id": "run-1", "timeout_seconds": 60, "after_revision": "rev-1", "until": until,
    }) == {"ok": True, "data": service.wait_run.return_value}
    assert service.mock_calls == [
        call.get_run_progress("run-1", refresh_state=True),
        call.wait_run("run-1", timeout_seconds=60, after_revision="rev-1", until=until),
    ]


def test_mcp_wait_defaults(service):
    payload = mcp_call(build_server(), "compute_wait_run", {"run_id": "run-1"})
    assert payload["data"]["timed_out"] is True
    assert service.mock_calls == [
        call.wait_run("run-1", timeout_seconds=30, after_revision=None, until="change"),
    ]


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_zero_wait_timeout_forwards_cache_only_boundary(service, surface):
    if surface == "cli":
        result = cli("run", "wait", "run-1", "--timeout", "0")
        assert result.exit_code == 0, result.output
    else:
        payload = mcp_call(build_server(), "compute_wait_run", {"run_id": "run-1", "timeout_seconds": 0})
        assert payload["ok"] is True
    assert service.mock_calls == [
        call.wait_run("run-1", timeout_seconds=0, after_revision=None, until="change"),
    ]


@pytest.mark.parametrize("timeout", ["-1", "60.1", "nan", "inf", "-inf"])
def test_cli_rejects_unbounded_or_invalid_wait(service, timeout):
    result = cli("run", "wait", "run-1", "--timeout", timeout)
    assert result.exit_code == 2, result.output
    assert service.mock_calls == []


@pytest.mark.parametrize("timeout", [-1, 60.1, float("nan"), float("inf"), None, True, "30"])
def test_mcp_rejects_unbounded_or_invalid_wait(service, timeout):
    with pytest.raises(ToolError, match="validation error"):
        mcp_call(build_server(), "compute_wait_run", {"run_id": "run-1", "timeout_seconds": timeout})
    assert service.mock_calls == []


def test_cli_rejects_unknown_wait_condition(service):
    result = cli("run", "wait", "run-1", "--until", "anything")
    assert result.exit_code == 2
    assert service.mock_calls == []


def test_mcp_rejects_unknown_wait_condition(service):
    with pytest.raises(ToolError, match="validation error"):
        mcp_call(build_server(), "compute_wait_run", {"run_id": "run-1", "until": "anything"})
    assert service.mock_calls == []


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_plan_defaults_are_none_not_walltime_or_eta(service, surface):
    if surface == "cli":
        result = cli("run", "plan", "--project", "demo", "--minutes", "120")
        assert result.exit_code == 0, result.output
        request = service.plan_run.call_args.args[0].model_dump()
    else:
        payload = mcp_call(build_server(), "compute_plan_run", {"project_id": "demo", "time_minutes": 120})
        assert payload["ok"] is True
        request = service.plan_run.call_args.args[0]
    assert request["pilot_seconds"] is None
    assert request["runtime_budget_seconds"] is None
    assert request["requirements"]["time_minutes"] == 120
    assert len(service.mock_calls) == 1


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_plan_preserves_params_and_resource_semantics(service, surface):
    if surface == "cli":
        result = cli(
            "run", "plan", "--project", "demo", "--node", "local", "--env", "py",
            "--command", "python train.py", "--gpus", "2", "--cpus", "8", "--tasks", "3",
            "--mem", "32", "--gpu-mem", "16", "--minutes", "90", "--partition", "gpu",
            "--allow-shared-gpu", "--pilot-seconds", "25", "--runtime-budget-seconds", "120.5",
            "--param", "epochs=20", "--param", "rate=0.5", "--param", "dry=false",
            "--param", "label=demo", "--param", "optional=null",
        )
        assert result.exit_code == 0, result.output
        request = service.plan_run.call_args.args[0].model_dump()
    else:
        payload = mcp_call(build_server(), "compute_plan_run", {
            "project_id": "demo", "node_id": "local", "env_id": "py",
            "command_override": "python train.py", "gpus": 2, "cpus": 8, "tasks": 3,
            "memory_gb": 32, "gpu_memory_gb": 16, "time_minutes": 90, "partition": "gpu",
            "allow_shared_gpu": True, "pilot_seconds": 25, "runtime_budget_seconds": 120.5,
            "params": {"epochs": 20, "rate": 0.5, "dry": False, "label": "demo", "optional": None},
        })
        assert payload["ok"] is True
        request = service.plan_run.call_args.args[0]
    assert request["project_id"] == "demo"
    assert request["node_id"] == "local"
    assert request["env_id"] == "py"
    assert request["command_override"] == "python train.py"
    assert request["params"] == {"epochs": 20, "rate": 0.5, "dry": False, "label": "demo", "optional": None}
    assert request["pilot_seconds"] == 25
    assert request["runtime_budget_seconds"] == 120.5
    assert request["requirements"] == {
        "gpus": 2, "cpus": 8, "tasks": 3, "memory_gb": 32, "gpu_memory_gb": 16,
        "time_minutes": 90, "partition": "gpu", "allow_shared_gpu": True,
    }
    assert len(service.mock_calls) == 1


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--pilot-seconds", v) for v in ("0", "301", "2.5")]
    + [("--runtime-budget-seconds", v) for v in ("0", "-1", "nan", "inf")],
)
def test_cli_rejects_invalid_plan_limits(service, flag, value):
    result = cli("run", "plan", "--project", "demo", flag, value)
    assert result.exit_code == 2, result.output
    assert service.mock_calls == []


@pytest.mark.parametrize(
    ("key", "value"),
    [("pilot_seconds", v) for v in (0, 301, 2.5, True, "20")]
    + [("runtime_budget_seconds", v) for v in (0, -1, float("nan"), float("inf"), True, "20")],
)
def test_mcp_rejects_invalid_plan_limits(service, key, value):
    with pytest.raises(ToolError, match="validation error"):
        mcp_call(build_server(), "compute_plan_run", {"project_id": "demo", key: value})
    assert service.mock_calls == []


def test_progress_specs_cli_and_mcp_schema(service):
    expected = {
        "compute_get_run_progress": ("get_run_progress", ("run", "progress")),
        "compute_wait_run": ("wait_run", ("run", "wait")),
    }
    assert len(TOOL_SPECS) == 29
    specs = {spec.name: spec for spec in TOOL_SPECS}
    tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}
    assert set(tools) == set(specs)
    for name, (method, path) in expected.items():
        spec = specs[name]
        assert spec.service_method == method
        assert spec.cli_path == path
        assert spec.read_only is True
        assert tools[name].annotations.model_dump(by_alias=True)["readOnlyHint"] is True
        command = typer.main.get_command(app)
        for part in path:
            command = command.commands[part]
        assert command is not None
    schema = tools["compute_wait_run"].model_dump(by_alias=True)["inputSchema"]
    assert schema["required"] == ["run_id"]
    properties = schema["properties"]
    assert properties["timeout_seconds"]["default"] == 30
    assert properties["timeout_seconds"]["maximum"] == 60
    assert properties["timeout_seconds"]["minimum"] == 0
    assert properties["after_revision"]["default"] is None
    assert properties["until"]["default"] == "change"
    assert set(properties["until"]["enum"]) == {"change", "terminal", "phase", "alert"}
    plan = tools["compute_plan_run"].model_dump(by_alias=True)["inputSchema"]["properties"]
    assert plan["pilot_seconds"]["default"] is None
    assert plan["runtime_budget_seconds"]["default"] is None
    assert "background" in specs["compute_wait_run"].description.lower()
    assert service.mock_calls == []


@pytest.mark.parametrize("command", ["progress", "wait"])
@pytest.mark.parametrize("known", [True, False])
def test_human_progress_is_compact_and_unknown_explicit(service, command, known):
    evidence = {
        "run_id": "run-1", "revision": "rev-2", "phase": "train", "completed": 20,
        "total": 100, "fraction": 0.2, "rate_per_second": 2, "elapsed_seconds": 10,
        "eta": {"lower_seconds": 35, "upper_seconds": 50, "scope": "phase",
                "confidence": "low", "reason": "warmup"} if known else None,
        "reason": "insufficient evidence", "alerts": ["stalled"],
        "last_report_at": "report-time", "last_advance_at": "advance-time",
        "samples": [{"secretly_long_sample": "x" * 5000}] * 100,
    }
    getattr(service, "get_run_progress" if command == "progress" else "wait_run").return_value = evidence
    result = cli("run", command, "run-1", as_json=False)
    assert result.exit_code == 0, result.output
    for text in ("train", "20 / 100", "fraction", "rate", "elapsed", "ETA", "scope",
                 "confidence", "reason", "stalled", "report-time", "advance-time", "rev-2"):
        assert text in result.output
    assert ("35..50 seconds" if known else "不可估") in result.output
    assert "secretly_long_sample" not in result.output
    assert len(result.output) < 2000
    assert len(service.mock_calls) == 1


def test_human_progress_uses_service_eta_range_and_age_fields(service):
    service.get_run_progress.return_value = {
        "run_id": "run-1", "status": "progressing", "phase": "train", "completed": 20,
        "total": 100, "fraction": 0.2, "rate_per_second": 2, "elapsed_seconds": 10,
        "eta_seconds": 40, "eta_range_seconds": [35, 50], "eta_scope": "phase",
        "confidence": "low", "reason": "rolling median", "alerts": [],
        "last_report_age_seconds": 1.5, "last_advance_age_seconds": 4.5,
    }
    result = cli("run", "progress", "run-1", as_json=False)
    assert result.exit_code == 0, result.output
    for text in ("progressing", "35..50 seconds", "phase", "rolling median",
                 "last report age seconds", "1.5", "last advance age seconds", "4.5"):
        assert text in result.output
    assert "不可估" not in result.output
    assert service.mock_calls == [call.get_run_progress("run-1", refresh_state=True)]


@pytest.mark.parametrize("with_progress", [True, False])
def test_human_run_status_keeps_existing_fields_and_optional_progress(service, with_progress):
    payload = {
        "run_id": "run-1", "state": "running", "node_id": "local", "executor": "local",
        "handle": {}, "exit_code": None, "created_at": "created-time", "submitted_at": "submit-time",
        "finished_at": None, "log_path": "run.log", "error": None,
        "git_commit": "1234567890abcdef", "git_dirty": False,
        "events": [{"at": "event-time", "kind": "status", "message": "running locally"}],
    }
    if with_progress:
        payload["progress"] = {"phase": "train", "eta_range_seconds": [35, 50], "eta_scope": "phase"}
    service.get_run.return_value = payload
    result = cli("run", "status", "run-1", as_json=False)
    assert result.exit_code == 0, result.output
    for text in ("running", "local", "created-time", "submit-time", "run.log",
                 "1234567890ab", "event-time", "running locally"):
        assert text in result.output
    assert ("35..50 seconds" in result.output) is with_progress
    assert service.mock_calls == [call.get_run("run-1", refresh=True)]


@pytest.mark.parametrize("surface", ["cli", "mcp"])
@pytest.mark.parametrize("method", ["get_run_progress", "wait_run"])
def test_progress_errors_keep_structured_code(service, surface, method):
    getattr(service, method).side_effect = NotFound("unknown run")
    if surface == "cli":
        result = cli("run", "progress" if method == "get_run_progress" else "wait", "missing")
        assert result.exit_code != 0
        payload = json.loads(result.output)
    else:
        payload = mcp_call(build_server(), "compute_" + method, {"run_id": "missing"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert len(service.mock_calls) == 1
