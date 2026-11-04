"""Observation truth: freshness, error lifecycle, diagnosable failures, honest audit.

A saved run state is a *claim* about a node, not a fact.  These tests pin the
three things a reader (CLI, MCP, dashboard) needs in order to not present a
stale claim as the current truth:

* ``observed_age_seconds`` / ``stale`` on every run payload,
* an observation error that is cleared once a probe succeeds,
* ssh's own stderr kept when the invocation fails before the remote runs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import make_home

import rcf.runs.manager as manager_module
from rcf.core.config import Config
from rcf.core.ids import now_utc
from rcf.core.models import (
    EnvironmentPlan,
    JobHandle,
    JobStatus,
    PlannedCommand,
    RunEvent,
    RunRecord,
    RunState,
)
from rcf.core.runner import CommandResult, classify_ssh_failure
from rcf.mcp.server import TOOL_SPECS, build_server
from rcf.service import RcfService

SETTINGS = """\
[security]
allow_execute = true
allow_environment_mutation = false
allow_system_admin = false
require_confirmation = true

[runs]
stale_after_seconds = 60
"""


@pytest.fixture
def service(tmp_path):
    home = make_home(
        tmp_path,
        settings=SETTINGS,
        nodes='[nodes.test]\nkind = "local"\n',
        environments='[environments.demo]\nkind = "system"\npython = "python3"\n',
    )
    svc = RcfService(Config.load(home), actor="test")
    yield svc
    svc.close()


def save_run(
    service: RcfService,
    *,
    run_id: str = "run_observed",
    state: RunState = RunState.RUNNING,
    age: timedelta = timedelta(days=3),
    with_handle: bool = False,
) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        node_id="test",
        executor="local",
        state=state,
        created_at=now_utc() - timedelta(days=4),
        submitted_at=now_utc() - timedelta(days=4),
        last_observed_at=now_utc() - age,
        state_observed_at=now_utc() - age,
        command=PlannedCommand(argv=["true"], workdir="/tmp"),
        handle=(
            JobHandle(kind="local", node_id="test", workdir="/tmp/run")
            if with_handle
            else None
        ),
    )
    service.store.insert_run(record)
    return record


# -- 1. freshness ---------------------------------------------------------- #


def test_stale_run_is_flagged_with_its_observation_age(service):
    save_run(service, age=timedelta(days=3))

    payload = service.list_runs()[0]

    assert payload["stale"] is True
    assert payload["observed_age_seconds"] >= 3 * 24 * 3600
    assert payload["stale_after_seconds"] == 60


def test_freshly_observed_run_is_not_stale(service):
    save_run(service, age=timedelta(seconds=2))

    payload = service.get_run("run_observed", refresh=False)

    assert payload["stale"] is False
    assert payload["observed_age_seconds"] < 60


def test_terminal_run_is_never_stale(service):
    # A finished run cannot become "more finished": age is irrelevant, and
    # flagging it would make every old successful run look unhealthy.
    save_run(service, state=RunState.SUCCEEDED, age=timedelta(days=30))

    payload = service.get_run("run_observed", refresh=False)

    assert payload["stale"] is False
    assert payload["observed_age_seconds"] >= 30 * 24 * 3600


def test_unsubmitted_run_reports_no_observation_age(service):
    record = RunRecord(
        run_id="run_planned",
        node_id="test",
        executor="local",
        state=RunState.PLANNED,
        created_at=now_utc(),
        command=PlannedCommand(argv=["true"], workdir="/tmp"),
    )
    service.store.insert_run(record)

    payload = service.get_run("run_planned", refresh=False)

    assert payload["observed_age_seconds"] is None
    assert payload["stale"] is False


# -- 2. error lifecycle ---------------------------------------------------- #


def test_successful_probe_clears_the_previous_observation_error(service, monkeypatch):
    save_run(service, age=timedelta(seconds=5), with_handle=True)
    service.store.record_observation_error(
        "run_observed", "observation failed: ssh command failed on 'test': exit code 255"
    )
    assert service.store.get_run("run_observed").error

    class Recovered:
        def status(self, handle):  # noqa: ARG002 - executor contract
            return JobStatus(
                state=RunState.SUCCEEDED,
                exit_code=0,
                detail="exit_code file present",
                observed_at=now_utc(),
                finished_at=now_utc(),
            )

    monkeypatch.setattr(manager_module, "build_executor", lambda *args, **kwargs: Recovered())

    record = service.runs.refresh("run_observed")

    assert record.state is RunState.SUCCEEDED
    assert record.error is None
    assert service.store.get_run("run_observed").error is None
    kinds = [event.kind for event in service.store.list_events("run_observed")]
    assert "observe_recovered" in kinds


def test_failed_probe_keeps_the_last_known_state_and_the_reason(service, monkeypatch):
    # The state was confirmed 30 minutes ago; the probe attempt happening now
    # must not make that claim look fresh.
    save_run(service, age=timedelta(minutes=30), with_handle=True)

    class Unreachable:
        def status(self, handle):  # noqa: ARG002 - executor contract
            raise RuntimeError("ssh command failed on 'test': exit code 255")

    monkeypatch.setattr(manager_module, "build_executor", lambda *args, **kwargs: Unreachable())

    record = service.runs.refresh("run_observed")

    assert record.state is RunState.RUNNING
    assert "exit code 255" in (record.error or "")
    payload = service.get_run("run_observed", refresh=False)
    assert payload["stale"] is True  # the claim just got older, and it says so


def test_get_run_lifts_the_latest_diagnosis_to_the_top_level(service):
    save_run(service, state=RunState.FAILED, age=timedelta(seconds=5))
    assert service.get_run("run_observed", refresh=False)["diagnosis"] is None

    service.store.add_event(
        RunEvent(run_id="run_observed", at=now_utc(), kind="diagnosis", message="exit 137: killed")
    )
    service.store.add_event(
        RunEvent(
            run_id="run_observed",
            at=now_utc(),
            kind="diagnosis",
            message="No module named: numpy",
            data={"exit_code": 1},
        )
    )

    payload = service.get_run("run_observed", refresh=False)

    assert payload["diagnosis"]["message"] == "No module named: numpy"
    assert payload["diagnosis"]["data"] == {"exit_code": 1}
    # The primary error field is not repurposed: callers that already read it
    # keep working, and the diagnosis is additional structured context.
    assert payload["error"] is None


# -- 3. diagnosable ssh failures ------------------------------------------- #


def test_ssh_failure_keeps_stderr_for_the_caller():
    result = CommandResult(
        argv=("ssh",),
        returncode=255,
        stdout="",
        stderr="CreateProcessW failed: environment variable PROGRAMDATA is missing",
    )

    exc = classify_ssh_failure(result, alias="mac")

    assert "PROGRAMDATA" in exc.detail["stderr"]
    assert exc.detail["returncode"] == 255


def test_mcp_error_payload_exposes_ssh_stderr():
    result = CommandResult(argv=("ssh",), returncode=255, stdout="", stderr="no matching host key type")
    exc = classify_ssh_failure(result, alias="mac")

    from rcf.service import error_payload

    payload = error_payload(exc)
    assert payload["code"] == "executor_error"
    assert "no matching host key type" in payload["detail"]["stderr"]


# -- 4. audit says what was actually enforced ------------------------------ #


def test_environment_apply_audits_the_enforced_tier(service, monkeypatch):
    plan = EnvironmentPlan(env_id="demo", node_id="test", action="none", requires_authorization=True)
    monkeypatch.setattr(service.environments, "plan", lambda env, node: plan)
    monkeypatch.setattr(service.environments, "apply", lambda plan, env, node, confirmed: [])

    service.apply_environment("demo", "test", confirmed=True)

    entry = next(a for a in service.store.list_audits() if a["operation"] == "environment.apply")
    assert entry["tier"] == "execute"
    assert entry["detail"]["gate"] == "path ownership + explicit confirmation"


def test_doctor_marks_the_legacy_flag_as_not_a_gate(service):
    report = service.doctor()

    check = next(c for c in report["checks"] if c["name"] == "security.allow_environment_mutation")
    assert check["status"] == "info"
    assert "not a gate" in check["detail"]


def test_doctor_warns_when_a_local_project_path_is_missing(tmp_path):
    missing = str(tmp_path / "missing-project").replace("\\", "/")
    home = make_home(
        tmp_path,
        nodes='[nodes.test]\nkind = "local"\n',
        projects=f'[projects.demo]\nentrypoint = "true"\npaths = {{ "test" = "{missing}" }}\n',
    )
    svc = RcfService(Config.load(home), actor="test")
    try:
        report = svc.doctor()
        check = next(c for c in report["checks"] if c["name"] == "project_paths")
        assert check["status"] == "warn"
        assert "missing-project" in check["detail"]
    finally:
        svc.close()


# -- 5. MCP surface reaches environment installs --------------------------- #


def test_mcp_exposes_environment_plan_and_apply(tmp_path):
    home = make_home(
        tmp_path,
        nodes='[nodes.test]\nkind = "local"\n',
        environments='[environments.demo]\nkind = "system"\n',
    )
    tools = {tool.name: tool for tool in asyncio.run(build_server(str(home)).list_tools())}

    assert "compute_plan_environment" in tools
    assert "compute_apply_environment" in tools
    specs = {spec.name: spec for spec in TOOL_SPECS}
    assert specs["compute_plan_environment"].read_only is True
    assert specs["compute_apply_environment"].read_only is False
    assert specs["compute_apply_environment"].service_method == "apply_environment"


def call_tool(server, name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


def test_mcp_plan_environment_is_read_only(tmp_path):
    home = make_home(
        tmp_path,
        nodes='[nodes.test]' + chr(10) + 'kind = "local"',
        environments='[environments.demo]' + chr(10) + 'kind = "system"',
    )
    server = build_server(str(home))

    payload = call_tool(server, "compute_plan_environment", {"env_id": "demo", "node_id": "test"})

    assert payload["ok"] is True
    assert payload["data"]["action"] in {"none", "create", "install", "blocked"}
    assert "steps" in payload["data"] and "blockers" in payload["data"]


def test_mcp_apply_environment_requires_confirmation(tmp_path):
    home = make_home(
        tmp_path,
        nodes='[nodes.test]' + chr(10) + 'kind = "local"',
        environments='[environments.demo]' + chr(10) + 'kind = "system"',
    )
    server = build_server(str(home))

    refused = call_tool(server, "compute_apply_environment", {"env_id": "demo", "node_id": "test"})

    assert refused["ok"] is False
    assert refused["error"]["code"] == "permission_denied"
