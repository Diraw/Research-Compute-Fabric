"""Storage surfaces forward only to mocked services; no nodes or research files touched."""

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
from rcf.core.errors import ApprovalRequired, SpecError
from rcf.mcp.server import TOOL_SPECS, ToolRuntime, build_server

cli_module = importlib.import_module("rcf.cli.main")

SURFACES = [
    ("compute_scan_storage", "scan_storage", ("storage", "scan"), True),
    ("compute_report_storage", "report_storage", ("storage", "report"), True),
    ("compute_check_storage", "check_storage", ("storage", "check"), True),
    ("compute_plan_storage_cleanup", "plan_storage_cleanup", ("storage", "plan"), True),
    ("compute_plan_storage_adoption", "plan_storage_adoption", ("storage", "adopt"), True),
    ("compute_get_storage_plan", "get_storage_plan", ("storage", "show"), True),
    ("compute_reconcile_storage_plan", "reconcile_storage_plan", ("storage", "reconcile"), False),
    ("compute_apply_storage_plan", "apply_storage_plan", ("storage", "apply"), False),
    ("compute_pin_run", "pin_run", ("run", "pin"), False),
]


@pytest.fixture
def service(monkeypatch):
    fake = Mock()
    for _, method, _, _ in SURFACES:
        getattr(fake, method).return_value = {"method": method, "plan_id": "storage-plan"}
    monkeypatch.setattr(cli_module, "_service", lambda state: fake)
    monkeypatch.setattr(ToolRuntime, "service", property(lambda self: fake))
    return fake


def cli(*args, as_json=True):
    return CliRunner().invoke(app, [*(["--json"] if as_json else []), *args])


def mcp_call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


@pytest.mark.parametrize(
    ("args", "method", "expected"),
    [
        (["storage", "scan", "demo", "--node", "local"], "scan_storage", call("demo", "local")),
        (["storage", "report", "demo"], "report_storage", call("demo", None, largest=20)),
        (["storage", "report", "demo", "--node", "local", "--largest", "4"],
         "report_storage", call("demo", "local", largest=4)),
        (["storage", "check", "demo"], "check_storage", call("demo", None)),
        (["storage", "check", "demo", "--node", "local"], "check_storage", call("demo", "local")),
        (["storage", "plan", "demo", "--node", "local"], "plan_storage_cleanup",
         call("demo", "local", kind="disposable", root_id=None, source_node_id=None, run_id=None)),
        (["storage", "plan", "demo", "--node", "local", "--kind", "replica", "--root", "data",
          "--from", "source", "--run", "run-1"], "plan_storage_cleanup",
         call("demo", "local", kind="replica", root_id="data", source_node_id="source", run_id="run-1")),
        (["storage", "adopt", "demo", "--node", "local", "--root", "data",
          "--path", "a file.txt", "--path", "sub/b.txt"], "plan_storage_adoption",
         call("demo", "local", root_id="data", paths=["a file.txt", "sub/b.txt"], kind="output",
              run_id=None, source_node_id=None)),
        (["storage", "adopt", "demo", "--node", "local", "--root", "code",
          "--path", "old.py", "--kind", "code", "--from", "source"], "plan_storage_adoption",
         call("demo", "local", root_id="code", paths=["old.py"], kind="code",
              run_id=None, source_node_id="source")),
        (["storage", "adopt", "demo", "--node", "local", "--root-id", "data",
          "--path", "cache.bin", "--kind", "cache", "--run", "run-1"], "plan_storage_adoption",
         call("demo", "local", root_id="data", paths=["cache.bin"], kind="cache",
              run_id="run-1", source_node_id=None)),
        (["storage", "show", "plan-1"], "get_storage_plan", call("plan-1")),
        (["storage", "reconcile", "plan-1"], "reconcile_storage_plan", call("plan-1", confirm=False)),
        (["storage", "reconcile", "plan-1", "--yes"], "reconcile_storage_plan",
         call("plan-1", confirm=True)),
        (["storage", "apply", "plan-1"], "apply_storage_plan", call("plan-1", confirm=False, resume=False)),
        (["storage", "apply", "plan-1", "--resume"], "apply_storage_plan",
         call("plan-1", confirm=False, resume=True)),
        (["storage", "apply", "plan-1", "--yes"], "apply_storage_plan", call("plan-1", confirm=True, resume=False)),
        (["storage", "apply", "plan-1", "--resume", "--yes"], "apply_storage_plan",
         call("plan-1", confirm=True, resume=True)),
        (["run", "pin", "run-1"], "pin_run", call("run-1", pinned=True, reason="", confirm=False)),
        (["run", "pin", "run-1", "--reason", "keep evidence", "--yes"],
         "pin_run", call("run-1", pinned=True, reason="keep evidence", confirm=True)),
        (["run", "unpin", "run-1", "--yes"], "pin_run",
         call("run-1", pinned=False, reason="", confirm=True)),
    ],
)
def test_cli_storage_forwarding(service, args, method, expected):
    result = cli(*args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"method": method, "plan_id": "storage-plan"}
    assert service.mock_calls == [getattr(call, method)(*expected.args, **expected.kwargs)]


@pytest.mark.parametrize("kind", ["unknown", "input", "output", "code"])
def test_cli_cleanup_rejects_non_cleanup_kind(service, kind):
    result = cli("storage", "plan", "demo", "--node", "local", "--kind", kind)
    assert result.exit_code == 2
    service.assert_not_called()
    assert service.mock_calls == []


def test_cli_adoption_requires_explicit_paths_and_known_kind(service):
    base = ["storage", "adopt", "demo", "--node", "local", "--root", "data"]
    assert cli(*base).exit_code == 2
    assert cli(*base, "--path", "a", "--kind", "unknown").exit_code == 2
    assert service.mock_calls == []


@pytest.mark.parametrize("kind", ["code", "input", "output", "disposable", "cache"])
def test_cli_adoption_accepts_only_declared_kinds(service, kind):
    result = cli("storage", "adopt", "demo", "--node", "local", "--root", "data",
                 "--path", "selected", "--kind", kind)
    assert result.exit_code == 0, result.output
    assert service.plan_storage_adoption.call_args.kwargs["kind"] == kind
    service.apply_storage_plan.assert_not_called()


def test_cli_plan_human_output_is_reviewable_and_never_applies(service):
    result = cli("storage", "plan", "demo", "--node", "local", as_json=False)
    assert result.exit_code == 0, result.output
    assert "not applied" in result.output
    assert "No files changed" in result.output
    assert "rcf storage apply storage-plan --yes" in result.output
    service.apply_storage_plan.assert_not_called()


@pytest.mark.parametrize("surface", ["cli-json", "cli-human", "mcp"])
def test_storage_show_preserves_all_read_only_evidence(service, surface):
    evidence = {
        "plan": {
            "plan_id": "plan-1", "status": "unknown", "owner_id": "run-1",
            "execution": {"token": "owner-token", "controller": "controller-1", "pid": 123,
                          "heartbeat_at": "2026-11-01T02:00:00Z", "stage": "outcome_unknown"},
            "protection": {"pinned": True, "reason": "keep evidence"},
            "entries": [{"path": "nested/file.txt", "sha256": "digest"}],
        },
        "journal": [{"path": "nested/file.txt", "status": "blocked", "detail": {"reason": "pinned"}}],
        "guards": [{"owner_id": "plan-1", "root": "/declared/root", "kind": "protection"}],
        "note": "Read-only evidence. Heartbeat age does not authorize takeover.",
    }
    service.get_storage_plan.return_value = evidence
    if surface == "mcp":
        assert mcp_call(build_server(), "compute_get_storage_plan", {"plan_id": "plan-1"}) == {
            "ok": True, "data": evidence,
        }
    else:
        result = cli("storage", "show", "plan-1", as_json=surface == "cli-json")
        assert result.exit_code == 0, result.output
        output = result.output
        if surface == "cli-human":
            title, output = output.split("\n", 1)
            assert "read-only" in title
        assert json.loads(output) == evidence
    assert service.mock_calls == [call.get_storage_plan("plan-1")]


def test_storage_show_requires_plan_id(service):
    assert cli("storage", "show").exit_code == 2
    with pytest.raises(ToolError, match="validation error"):
        asyncio.run(build_server().call_tool("compute_get_storage_plan", {}))
    assert service.mock_calls == []


@pytest.mark.parametrize(
    ("name", "arguments", "method", "expected"),
    [
        ("compute_scan_storage", {"project_id": "demo", "node_id": "local"},
         "scan_storage", call("demo", "local")),
        ("compute_report_storage", {"project_id": "demo"},
         "report_storage", call("demo", None, largest=20)),
        ("compute_report_storage", {"project_id": "demo", "node_id": "local", "largest": 3},
         "report_storage", call("demo", "local", largest=3)),
        ("compute_check_storage", {"project_id": "demo"}, "check_storage", call("demo", None)),
        ("compute_plan_storage_cleanup", {"project_id": "demo", "node_id": "local"},
         "plan_storage_cleanup",
         call("demo", "local", kind="disposable", root_id=None, source_node_id=None, run_id=None)),
        ("compute_plan_storage_cleanup", {"project_id": "demo", "node_id": "local", "kind": "stale-code",
                                         "root_id": "code", "source_node_id": "source", "run_id": "run-1"},
         "plan_storage_cleanup",
         call("demo", "local", kind="stale-code", root_id="code", source_node_id="source", run_id="run-1")),
        ("compute_plan_storage_adoption", {"project_id": "demo", "node_id": "local", "root_id": "data",
                                          "paths": ["a file.txt", "sub/b.txt"]},
         "plan_storage_adoption",
         call("demo", "local", root_id="data", paths=["a file.txt", "sub/b.txt"], kind="output",
              run_id=None, source_node_id=None)),
        ("compute_plan_storage_adoption", {"project_id": "demo", "node_id": "local", "root_id": "code",
                                          "paths": ["old.py"], "kind": "code", "source_node_id": "source"},
         "plan_storage_adoption",
         call("demo", "local", root_id="code", paths=["old.py"], kind="code",
              run_id=None, source_node_id="source")),
        ("compute_get_storage_plan", {"plan_id": "plan-1"}, "get_storage_plan", call("plan-1")),
        ("compute_reconcile_storage_plan", {"plan_id": "plan-1"},
         "reconcile_storage_plan", call("plan-1", confirm=False)),
        ("compute_reconcile_storage_plan", {"plan_id": "plan-1", "confirm": True},
         "reconcile_storage_plan", call("plan-1", confirm=True)),
        ("compute_apply_storage_plan", {"plan_id": "plan-1"},
         "apply_storage_plan", call("plan-1", confirm=False, resume=False)),
        ("compute_apply_storage_plan", {"plan_id": "plan-1", "resume": True},
         "apply_storage_plan", call("plan-1", confirm=False, resume=True)),
        ("compute_apply_storage_plan", {"plan_id": "plan-1", "confirm": True},
         "apply_storage_plan", call("plan-1", confirm=True, resume=False)),
        ("compute_apply_storage_plan", {"plan_id": "plan-1", "confirm": True, "resume": True},
         "apply_storage_plan", call("plan-1", confirm=True, resume=True)),
        ("compute_pin_run", {"run_id": "run-1"},
         "pin_run", call("run-1", pinned=True, reason="", confirm=False)),
        ("compute_pin_run", {"run_id": "run-1", "pinned": False, "reason": "reviewed", "confirm": True},
         "pin_run", call("run-1", pinned=False, reason="reviewed", confirm=True)),
    ],
)
def test_mcp_storage_forwarding(service, name, arguments, method, expected):
    payload = mcp_call(build_server(), name, arguments)
    assert payload == {"ok": True, "data": {"method": method, "plan_id": "storage-plan"}}
    assert service.mock_calls == [getattr(call, method)(*expected.args, **expected.kwargs)]


def test_storage_specs_cli_paths_schemas_and_annotations(service):
    specs = {spec.name: spec for spec in TOOL_SPECS}
    tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}
    assert len(specs) == len(TOOL_SPECS)
    assert set(tools) == set(specs)
    for name, method, path, read_only in SURFACES:
        spec = specs[name]
        assert spec.service_method == method
        assert spec.cli_path == path
        assert spec.read_only is read_only
        assert tools[name].annotations.model_dump(by_alias=True)["readOnlyHint"] is read_only
        command = typer.main.get_command(app)
        for part in path:
            command = command.commands[part]
    show = tools["compute_get_storage_plan"].model_dump(by_alias=True)["inputSchema"]
    assert show["required"] == ["plan_id"]
    assert set(show["properties"]) == {"plan_id"}
    adopt = tools["compute_plan_storage_adoption"].model_dump(by_alias=True)["inputSchema"]
    assert {"root_id", "paths"} <= set(adopt["required"])
    assert set(adopt["properties"]["kind"]["enum"]) == {"code", "input", "output", "disposable", "cache"}
    cleanup = tools["compute_plan_storage_cleanup"].model_dump(by_alias=True)["inputSchema"]
    assert set(cleanup["properties"]["kind"]["enum"]) == {"stale-code", "disposable", "replica", "cache"}
    reconcile = tools["compute_reconcile_storage_plan"].model_dump(by_alias=True)["inputSchema"]
    assert reconcile["required"] == ["plan_id"]
    assert set(reconcile["properties"]) == {"plan_id", "confirm"}
    for name in ("compute_apply_storage_plan", "compute_reconcile_storage_plan", "compute_pin_run"):
        schema = tools[name].model_dump(by_alias=True)["inputSchema"]
        assert schema["properties"]["confirm"]["default"] is False
    assert service.mock_calls == []


def test_mcp_adoption_rejects_implicit_paths_and_unknown_kind(service):
    server = build_server()
    base = {"project_id": "demo", "node_id": "local", "root_id": "data"}
    for arguments in (base, {**base, "paths": ["a"], "kind": "unknown"}):
        with pytest.raises(ToolError, match="validation error"):
            asyncio.run(server.call_tool("compute_plan_storage_adoption", arguments))
    assert service.mock_calls == []


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_storage_apply_preserves_approval_error(service, surface):
    service.apply_storage_plan.side_effect = ApprovalRequired("review and confirm this plan")
    if surface == "cli":
        result = cli("storage", "apply", "plan-1")
        assert result.exit_code == 7
        payload = json.loads(result.output)
    else:
        payload = mcp_call(build_server(), "compute_apply_storage_plan", {"plan_id": "plan-1"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    service.apply_storage_plan.assert_called_once_with("plan-1", confirm=False, resume=False)


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_storage_reconcile_preserves_approval_error(service, surface):
    service.reconcile_storage_plan.side_effect = ApprovalRequired("review and confirm reconciliation")
    if surface == "cli":
        result = cli("storage", "reconcile", "plan-1")
        assert result.exit_code == 7
        payload = json.loads(result.output)
    else:
        payload = mcp_call(build_server(), "compute_reconcile_storage_plan", {"plan_id": "plan-1"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert service.mock_calls == [call.reconcile_storage_plan("plan-1", confirm=False)]


@pytest.mark.parametrize("surface", ["cli-json", "cli-human", "mcp"])
def test_storage_reconcile_preserves_no_delete_result(service, surface):
    evidence = {
        "plan_id": "plan-1", "no-delete": True, "status": "superseded",
        "manifest": [{"path": "nested/file.txt", "observation": "changed"}],
        "guards_released": ["guard-1"],
    }
    service.reconcile_storage_plan.return_value = evidence
    if surface == "mcp":
        assert mcp_call(build_server(), "compute_reconcile_storage_plan", {
            "plan_id": "plan-1", "confirm": True,
        }) == {"ok": True, "data": evidence}
    else:
        result = cli("storage", "reconcile", "plan-1", "--yes", as_json=surface == "cli-json")
        assert result.exit_code == 0, result.output
        output = result.output
        if surface == "cli-human":
            title, output = output.split("\n", 1)
            assert "no deletion" in title
        assert json.loads(output) == evidence
    assert service.mock_calls == [call.reconcile_storage_plan("plan-1", confirm=True)]


def test_storage_reconcile_requires_plan_and_does_not_accept_resume(service):
    assert cli("storage", "reconcile").exit_code == 2
    assert cli("storage", "reconcile", "plan-1", "--resume", "--yes").exit_code == 2
    with pytest.raises(ToolError, match="validation error"):
        asyncio.run(build_server().call_tool("compute_reconcile_storage_plan", {}))
    assert service.mock_calls == []


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_pin_reason_is_validated_by_service(service, surface):
    service.pin_run.side_effect = SpecError("pin requires a nonempty reason")
    if surface == "cli":
        result = cli("run", "pin", "run-1", "--yes")
        assert result.exit_code == 2
        payload = json.loads(result.output)
    else:
        payload = mcp_call(build_server(), "compute_pin_run", {"run_id": "run-1", "confirm": True})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "spec_invalid"
    service.pin_run.assert_called_once_with("run-1", pinned=True, reason="", confirm=True)
