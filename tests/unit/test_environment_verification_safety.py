"""Arbitrary environment verification is never a read-only inspection."""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import make_home
from typer.testing import CliRunner

from rcf.cli.main import app
from rcf.core.config import Config
from rcf.core.errors import PermissionDenied
from rcf.core.models import EnvironmentSpec, NodeSpec
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.environments.manager import EnvironmentManager
from rcf.mcp.server import build_server
from rcf.service import RcfService


def probe():
    return CommandResult(argv=(), returncode=0, stdout=json.dumps({"python": "3.12.0", "packages": {}}))


def service(tmp_path, *, execute=True, confirmation=True):
    settings = ("[security]\n" + f"allow_execute = {str(execute).lower()}\n"
                + f"require_confirmation = {str(confirmation).lower()}\n")
    home = make_home(tmp_path, settings=settings, nodes='[nodes.local]\nkind="local"\n',
                     environments='[environments.demo]\nkind="system"\nverify_command="echo verified"\n')
    runner = ScriptedRunner([probe(), CommandResult(argv=(), returncode=0, stdout="verified")])
    return RcfService(Config.load(home), runner=runner), runner


def test_legacy_inspect_verify_cannot_bypass_disabled_execute(tmp_path):
    svc, runner = service(tmp_path, execute=False)
    with svc, pytest.raises(PermissionDenied):
        svc.inspect_environment("demo", "local", verify=True)
    assert runner.calls == []


def test_inspect_verify_needs_current_confirmation(tmp_path):
    svc, runner = service(tmp_path, confirmation=False)
    with svc, pytest.raises(PermissionDenied):
        svc.inspect_environment("demo", "local", verify=True)
    assert runner.calls == []


def test_low_level_verification_is_gated_before_probe(tmp_path):
    svc, runner = service(tmp_path, execute=False)
    with svc, pytest.raises(PermissionDenied):
        svc.environments.inspect(svc.config.environment("demo"), svc.config.node("local"), verify=True)
    assert runner.calls == []


def test_confirmed_verify_records_audit_and_runs(tmp_path):
    svc, runner = service(tmp_path)
    with svc:
        result = svc.verify_environment("demo", "local", confirm=True)
        assert result["state"] == "verified"
        assert len(runner.calls) == 2
        assert svc.store.list_audits()[0]["operation"] == "environment.verify"


def test_plain_inspection_does_not_execute_verify_command(tmp_path):
    svc, runner = service(tmp_path)
    with svc:
        assert svc.inspect_environment("demo", "local")["state"] == "compatible"
        assert len(runner.calls) == 1


def test_mcp_inspection_schema_is_read_only_and_has_no_verify_switch():
    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    assert "verify" not in tools["compute_inspect_environment"].input_schema["properties"]
    assert tools["compute_inspect_environment"].annotations.read_only_hint is True
    verify = tools["compute_verify_environment"]
    assert verify.annotations.read_only_hint is False
    assert verify.input_schema["properties"]["confirm"]["default"] is False


def test_cli_verify_without_yes_is_refused(tmp_path):
    svc, _ = service(tmp_path)
    home = svc.config.home
    svc.close()
    result = CliRunner().invoke(app, ["--home", str(home), "env", "verify", "demo", "--node", "local"])
    assert result.exit_code == 7


def test_direct_manager_verify_requires_confirmation(tmp_path):
    svc, runner = service(tmp_path)
    with svc:
        manager = EnvironmentManager(svc.config, runner)
        with pytest.raises(PermissionDenied):
            manager.inspect(EnvironmentSpec(id="demo", kind="system", verify_command="echo no"),
                            NodeSpec(id="local", kind="local"), verify=True)
    assert runner.calls == []
