"""Tracked environment applies: the MCP call returns before the install ends."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import Mock, call

import pytest
from conftest import make_home
from typer.testing import CliRunner

from rcf.cli.main import app
from rcf.core.config import Config
from rcf.core.errors import Conflict, PermissionDenied
from rcf.core.models import EnvironmentPlan, EnvironmentStep
from rcf.mcp.server import ToolRuntime, build_server
from rcf.service import RcfService


def make_service(tmp_path) -> RcfService:
    home = make_home(
        tmp_path,
        nodes='[nodes.test]\nkind = "local"\n',
        environments='[environments.demo]\nkind = "system"\n',
    )
    return RcfService(Config.load(home), actor="test")


def one_step_plan() -> EnvironmentPlan:
    return EnvironmentPlan(
        env_id="demo",
        node_id="test",
        action="install",
        steps=[EnvironmentStep(description="install", command=["true"])],
    )


def wait_for(state: RcfService, apply_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = state.get_environment_apply(apply_id)
        if record["state"] != "running":
            return record
        time.sleep(0.02)
    raise AssertionError(f"apply {apply_id} did not finish")


def test_apply_job_runs_off_the_call_and_records_each_step(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    plan = one_step_plan()
    monkeypatch.setattr(service.environments, "plan", lambda env, node: plan)
    monkeypatch.setattr(
        service.environments, "preflight_apply", lambda plan, env, node, confirmed: "/tmp/demo"
    )

    def fake_apply(plan, env, node, *, confirmed, on_step=None):
        result = {"description": "install", "command": ["true"], "returncode": 0, "output": "ok"}
        if on_step is not None:
            on_step(result)
        return [result]

    monkeypatch.setattr(service.environments, "apply", fake_apply)
    try:
        started = service.start_apply_environment("demo", "test", confirmed=True)
        assert started["state"] == "running"
        assert started["plan"]["action"] == "install"

        finished = wait_for(service, started["apply_id"])
        assert finished["state"] == "succeeded"
        assert finished["results"][0]["returncode"] == 0
        assert finished["error"] is None
    finally:
        service.close()


def test_apply_job_failure_is_captured_for_the_poller(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    plan = one_step_plan()
    monkeypatch.setattr(service.environments, "plan", lambda env, node: plan)
    monkeypatch.setattr(
        service.environments, "preflight_apply", lambda plan, env, node, confirmed: "/tmp/demo"
    )

    def failing_apply(plan, env, node, *, confirmed, on_step=None):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(service.environments, "apply", failing_apply)
    try:
        started = service.start_apply_environment("demo", "test", confirmed=True)
        finished = wait_for(service, started["apply_id"])
        assert finished["state"] == "failed"
        assert finished["error"]["code"] == "internal_error"
        assert "pip exploded" in finished["error"]["message"]
    finally:
        service.close()


def test_apply_job_refuses_a_second_worker_for_the_same_target(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    plan = one_step_plan()
    monkeypatch.setattr(service.environments, "plan", lambda env, node: plan)
    monkeypatch.setattr(
        service.environments, "preflight_apply", lambda plan, env, node, confirmed: "/tmp/demo"
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_apply(plan, env, node, *, confirmed, on_step=None):
        entered.set()
        assert release.wait(5)
        return [{"description": "install", "command": ["true"], "returncode": 0, "output": ""}]

    monkeypatch.setattr(service.environments, "apply", blocking_apply)
    try:
        started = service.start_apply_environment("demo", "test", confirmed=True)
        assert entered.wait(5)
        with pytest.raises(Conflict):
            service.start_apply_environment("demo", "test", confirmed=True)
        release.set()
        assert wait_for(service, started["apply_id"])["state"] == "succeeded"
        assert len(service.list_environment_applies()) == 1
    finally:
        release.set()
        service.close()


def test_stale_apply_is_failed_not_left_running_forever(tmp_path):
    service = make_service(tmp_path)
    try:
        service.store.create_environment_apply(
            apply_id="envapply_stale", env_id="demo", node_id="test", plan={}
        )
        with service.store._lock:
            service.store._conn.execute(
                "UPDATE environment_applies SET updated_at = ? WHERE apply_id = ?",
                ("2000-01-01T00:00:00+00:00", "envapply_stale"),
            )
            service.store._conn.commit()

        assert service.store.reap_stale_environment_applies(older_than_seconds=60) == 1
        record = service.get_environment_apply("envapply_stale")
        assert record["state"] == "failed"
        assert record["error"]["code"] == "stale_apply"
    finally:
        service.close()


def test_apply_job_requires_confirmation_before_creating_a_record(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service.environments, "plan", lambda env, node: one_step_plan())
    try:
        with pytest.raises(PermissionDenied):
            service.start_apply_environment("demo", "test")
        assert service.list_environment_applies() == []
    finally:
        service.close()


def test_mcp_and_cli_forward_the_job_surface(monkeypatch):
    record = {
        "apply_id": "apply-1",
        "env_id": "demo",
        "node_id": "test",
        "state": "succeeded",
        "started_at": "2026-11-19T10:00:00+00:00",
        "updated_at": "2026-11-19T10:01:00+00:00",
        "finished_at": "2026-11-19T10:01:00+00:00",
        "plan": {},
        "results": [],
        "error": None,
    }
    fake = Mock()
    fake.start_apply_environment.return_value = {**record, "state": "running"}
    fake.get_environment_apply.return_value = record
    fake.list_environment_applies.return_value = [record]
    monkeypatch.setattr(ToolRuntime, "service", property(lambda self: fake))

    server = build_server()
    result = asyncio.run(
        server.call_tool(
            "compute_apply_environment",
            {"env_id": "demo", "node_id": "test", "confirm": True},
        )
    )
    assert json.loads(result.content[0].text)["data"]["apply_id"] == "apply-1"
    assert fake.mock_calls == [
        call.start_apply_environment("demo", "test", confirmed=True)
    ]

    result = asyncio.run(
        server.call_tool("compute_get_environment_apply", {"apply_id": "apply-1"})
    )
    assert json.loads(result.content[0].text)["data"]["state"] == "succeeded"
    result = asyncio.run(server.call_tool("compute_list_environment_applies", {}))
    assert json.loads(result.content[0].text)["data"][0]["apply_id"] == "apply-1"

    import rcf.cli.main as cli_module

    monkeypatch.setattr(cli_module, "_service", lambda state: fake)
    invoked = CliRunner().invoke(
        app, ["env", "apply", "demo", "--node", "test", "--yes", "--background"]
    )
    assert invoked.exit_code == 0, invoked.output
    assert "apply-1" in invoked.output
    invoked = CliRunner().invoke(app, ["env", "apply-status", "apply-1"])
    assert invoked.exit_code == 0, invoked.output
    assert "succeeded" in invoked.output
    invoked = CliRunner().invoke(app, ["env", "applies"])
    assert invoked.exit_code == 0, invoked.output
    assert "apply-1" in invoked.output
