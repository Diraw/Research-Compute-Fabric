"""Measured progress workflows using real local jobs and temporary Git projects.

The worker imports only its copied reporter, never the controller's installed RCF.
Large formal workloads are planned, not dispatched; all submitted jobs are bounded.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from conftest import PYTHON, make_home, toml_path

from rcf.core.config import Config
from rcf.core.models import RunState
from rcf.core.runner import SubprocessRunner
from rcf.progress import reporter
from rcf.progress.collector import Collector
from rcf.progress.store import ProgressStore
from rcf.runs.manager import RunManager
from rcf.service import RcfService

pytestmark = pytest.mark.e2e

LARGE_TOTAL = 10_000
COMPLETION = "Every requested step completed and result.json written; process exits zero."
WORKER = '''import argparse
import json
import os
import pathlib
import time

from reporter import Reporter

parser = argparse.ArgumentParser()
parser.add_argument("--steps", type=int, required=True)
parser.add_argument("--mode", required=True)
args = parser.parse_args()
with Reporter(total=args.steps, min_interval_seconds=0.1) as progress:
    if args.mode == "invalid":
        progress.update(0, force=True)
        with open(os.environ["RCF_PROGRESS_FILE"], "a", encoding="utf-8") as stream:
            stream.write('{"not_a_progress_sample":true}\\n')
        while True:
            progress.heartbeat()
            time.sleep(0.2)
    elif args.mode == "heartbeat":
        progress.update(1, force=True)
        while True:
            progress.heartbeat()
            time.sleep(0.2)
    elif args.mode == "finished_but_alive":
        progress.update(args.steps, force=True, finished=True)
        while True:
            progress.heartbeat()
            time.sleep(0.2)
    else:
        progress.update(0, force=True)
        for step in range(1, args.steps + 1):
            time.sleep(0.05)
            progress.update(step, finished=step == args.steps)
        out = pathlib.Path(os.environ["RCF_OUTPUT_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps({"steps": args.steps}), encoding="utf-8")
        print("all steps completed", flush=True)
'''


@contextmanager
def local_service(tmp_path: Path) -> Iterator[RcfService]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "worker.py").write_text(WORKER, encoding="utf-8")
    assert reporter.__file__ is not None
    shutil.copyfile(reporter.__file__, project / "reporter.py")
    # Budgeted formal work needs immutable code identity. Disable hooks/signing
    # only for this temporary repository, without touching the user's Git config.
    (project / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    runner = SubprocessRunner()
    for args in (
        ["init"],
        ["add", "worker.py", "reporter.py", ".gitignore"],
        ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=", "-c", "user.name=RCF Test",
         "-c", "user.email=rcf-test@example.invalid", "commit", "-m", "temporary progress worker"],
    ):
        result = runner.run(["git", *args], cwd=project, timeout=30)
        assert result.returncode == 0, result.stderr
    home = make_home(
        tmp_path,
        legacy_progress=False,
        nodes='[nodes.local]\nkind = "local"\n',
        projects=f'''[projects.demo]
entrypoint = "{{python}} {{script}} --steps {{steps}} --mode {{mode}}"
script = "worker.py"
output_contract = "env"
env_id = "worker"
runtime_budget_seconds = 10000
paths = {{local = "{toml_path(project)}"}}
[projects.demo.progress]
mode = "steps"
total_param = "steps"
completion_condition = "{COMPLETION}"
heartbeat_timeout_seconds = 5
stall_timeout_seconds = 10
start_grace_seconds = 1
''',
        environments=f'''[environments.worker]
kind = "system"
python = "{toml_path(PYTHON)}"
python_version = ">=3.11"
''',
    )
    svc = RcfService(Config.load(home), runner=runner, actor="test-progress-e2e")
    try:
        assert svc.config.settings.progress.enforce_long_runs is True
        assert {node.kind.value for node in svc.config.nodes.values()} == {"local"}
        yield svc
    finally:
        try:
            for record in svc.store.list_runs(limit=100):
                if record.handle is None or record.state.terminal:
                    continue
                # An assertion can fail immediately after dispatch, before pid
                # publication. Retry cancellation rather than orphaning a worker.
                deadline = time.monotonic() + 12
                while not record.state.terminal and time.monotonic() < deadline:
                    record = svc.runs.refresh(record.run_id)
                    if not record.state.terminal:
                        svc.cancel_run(record.run_id, confirm=True)
                        record = svc.runs.get(record.run_id)
                    if not record.state.terminal:
                        time.sleep(0.05)
                assert record.state.terminal, f"temporary job still alive: {record.run_id}"
        finally:
            svc.close()


def request(steps: int = LARGE_TOTAL, mode: str = "advance", **kwargs: Any) -> dict[str, Any]:
    return {
        "project_id": "demo", "node_id": "local", "env_id": "worker",
        "params": {"steps": steps, "mode": mode},
        "requirements": {"cpus": 1, "gpus": 0, "tasks": 1},
        **kwargs,
    }


def checks(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in plan["checks"]}


def assert_executable(plan: dict[str, Any]) -> None:
    assert not [check for check in plan["checks"] if check["status"] == "fail"], plan["checks"]
    assert plan["executor"] == "local"
    assert plan["git_commit"] and plan["git_dirty"] is False


def submit(svc: RcfService, plan: dict[str, Any]) -> str:
    assert_executable(plan)
    submitted = svc.submit_run(plan["plan_id"], confirm=True)
    assert submitted["executor"] == "local"
    return submitted["run_id"]


def eventually(
    read: Callable[[], dict[str, Any]], predicate: Callable[[dict[str, Any]], bool],
    *, timeout: float = 8,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = read()
        if predicate(latest):
            return latest
        time.sleep(0.1)
    pytest.fail(f"condition not reached within {timeout}s; last observation: {latest}")


def test_measured_pilot_calibrates_matching_large_formal_budget_without_dispatch(tmp_path: Path) -> None:
    with local_service(tmp_path) as svc:
        formal_before = svc.plan_run(request())
        assert checks(formal_before)["runtime_calibration"]["status"] == "fail"
        assert svc.store.get_run(formal_before["run_id"]) is None

        pilot = svc.plan_run(request(pilot_seconds=2))
        assert pilot["progress_contract"]["total_param"] == "steps"
        assert pilot["progress_contract"]["completion_condition"] == COMPLETION
        assert pilot["progress_total"] == LARGE_TOTAL
        assert checks(pilot)["pilot_limit"]["data"]["automatic_termination"] is True
        assert pilot["command"]["env"]["RCF_PROGRESS_PILOT"] == "1"
        assert pilot["calibration_key"] == formal_before["calibration_key"]
        assert ProgressStore(svc.store).calibration(pilot["calibration_key"]) is None
        start = time.monotonic()
        run_id = submit(svc, pilot)
        live = eventually(
            lambda: svc.get_run(run_id, refresh=True),
            lambda view: view["state"] == "running" and view["progress"]["eta_seconds"] is not None,
        )
        assert 0 < live["progress"]["completed"] < LARGE_TOTAL
        assert live["progress"]["eta_seconds"] > 0
        assert live["progress"]["runtime_budget_seconds"] == 10_000
        assert svc.store.get_run(run_id).state is RunState.RUNNING
        assert ProgressStore(svc.store).calibration(pilot["calibration_key"]) is None

        terminal = eventually(
            lambda: svc.get_run(run_id, refresh=True),
            lambda view: view["state"] in {"failed", "succeeded", "cancelled", "lost"},
        )
        assert time.monotonic() - start < 12
        assert terminal["state"] == "failed" and terminal["exit_code"] == 124
        assert terminal["progress"]["run_state"] == "failed"
        run_dir = Path(terminal["handle"]["workdir"])
        watchdog = json.loads((run_dir / "pilot.json").read_text(encoding="utf-8"))
        assert watchdog["timed_out"] is True and watchdog["timeout_seconds"] == 2
        assert watchdog["exit_code"] == 124
        assert 1.9 <= watchdog["elapsed_seconds"] < 8
        samples = [json.loads(line) for line in (run_dir / "progress.jsonl").read_text(
            encoding="utf-8").splitlines()]
        assert len(samples) >= 3 and all(sample["total"] == LARGE_TOTAL for sample in samples)
        assert samples[-1]["completed"] < LARGE_TOTAL
        assert not samples[-1]["finished"]

        evidence = ProgressStore(svc.store).calibration(pilot["calibration_key"])
        assert evidence is not None
        assert evidence["run_id"] == run_id and evidence["evidence_verified"] is True
        assert evidence["pilot_seconds"] == 2 and evidence["total"] == LARGE_TOTAL
        assert evidence["samples"] == len(samples)
        assert 2 < evidence["estimated_total_seconds_upper"] < 10_000

        enough = svc.plan_run(request(runtime_budget_seconds=10_000))
        too_small = svc.plan_run(request(runtime_budget_seconds=1))
        for plan in (enough, too_small):
            assert plan["request"]["pilot_seconds"] is None
            assert plan["command"]["env"]["RCF_PROGRESS_PILOT"] == "0"
            assert plan["calibration_key"] == pilot["calibration_key"]
            assert plan["request"]["params"] == pilot["request"]["params"]
            assert plan["request"]["requirements"] == pilot["request"]["requirements"]
            assert plan["node_id"] == pilot["node_id"] and plan["env_id"] == pilot["env_id"]
            assert plan["env_fingerprint"] == pilot["env_fingerprint"]
            assert checks(plan)["runtime_calibration"]["status"] == "ok"
            assert checks(plan)["runtime_calibration"]["data"]["source_run_id"] == run_id
            assert svc.store.get_run(plan["run_id"]) is None
            assert not Path(plan["run_dir"]).exists()
        assert_executable(enough)
        assert checks(enough)["runtime_budget"]["status"] == "ok"
        assert checks(too_small)["runtime_budget"]["status"] == "fail"
        assert {item["name"] for item in too_small["checks"] if item["status"] == "fail"} == {
            "runtime_budget",
        }
        changed_total = svc.plan_run(request(steps=LARGE_TOTAL + 1))
        assert changed_total["calibration_key"] != pilot["calibration_key"]
        assert checks(changed_total)["runtime_calibration"]["status"] == "fail"
        assert [record.run_id for record in svc.store.list_runs()] == [run_id]


def test_small_calibrated_formal_run_really_completes_with_bounded_terminal_wait(tmp_path: Path) -> None:
    with local_service(tmp_path) as svc:
        workload = request(steps=12, requirements={"cpus": 1, "gpus": 0, "tasks": 1, "time_minutes": 1})
        pilot = svc.plan_run({**workload, "pilot_seconds": 2})
        pilot_id = submit(svc, pilot)
        measured = eventually(
            lambda: svc.get_run(pilot_id, refresh=True), lambda view: view["state"] == "succeeded",
        )
        assert measured["exit_code"] == 0
        evidence = ProgressStore(svc.store).calibration(pilot["calibration_key"])
        assert evidence is not None and evidence["run_id"] == pilot_id

        formal = svc.plan_run(workload)
        assert formal["calibration_key"] == pilot["calibration_key"]
        assert formal["request"]["requirements"]["time_minutes"] <= 5
        assert formal["request"]["pilot_seconds"] is None
        run_id = submit(svc, formal)
        start = time.monotonic()
        waited = svc.wait_run(run_id, timeout_seconds=8, until="terminal")
        assert time.monotonic() - start < 10
        assert waited["reason"] == "terminal" and waited["timed_out"] is False
        assert waited["progress"]["run_state"] == "succeeded"
        terminal = svc.get_run(run_id, refresh=True)
        assert terminal["exit_code"] == 0 and terminal["state"] == "succeeded"
        assert terminal["progress"]["completed"] == terminal["progress"]["total"] == 12
        assert terminal["progress"]["eta_seconds"] is None
        assert not (Path(terminal["handle"]["workdir"]) / "pilot.json").exists()
        result = Path(terminal["result_dir"]) / "result.json"
        assert json.loads(result.read_text(encoding="utf-8")) == {"steps": 12}
        assert "all steps completed" in svc.get_logs(run_id)["log"]


@pytest.mark.parametrize("mode", ["heartbeat", "invalid", "finished_but_alive"])
def test_reporting_anomalies_never_change_real_run_state(tmp_path: Path, mode: str) -> None:
    with local_service(tmp_path) as svc:
        pilot = svc.plan_run(request(mode=mode, pilot_seconds=30))
        run_id = submit(svc, pilot)
        if mode == "heartbeat":
            view = eventually(
                lambda: svc.get_run(run_id, refresh=True),
                lambda value: value["progress"]["status"] == "stalled", timeout=15,
            )
            assert "stalled_progress" in {alert["code"] for alert in view["progress"]["alerts"]}
            assert view["progress"]["last_report_age_seconds"] < 5
            assert view["progress"]["last_advance_age_seconds"] > 10
        elif mode == "invalid":
            view = eventually(
                lambda: svc.get_run(run_id, refresh=True),
                lambda value: "progress_evidence_invalid" in {
                    alert["code"] for alert in value["progress"]["alerts"]
                } and any("missing required progress fields" in error
                          for error in value["progress"]["evidence_errors"]),
            )
        else:
            view = eventually(
                lambda: svc.get_run(run_id, refresh=True),
                lambda value: value["progress"]["completed"] == LARGE_TOTAL,
            )
            assert view["progress"]["fraction"] == 1
            assert "awaiting run state" in view["progress"]["reason"]
        assert view["state"] == view["progress"]["run_state"] == "running"
        assert view["exit_code"] is None and view["finished_at"] is None
        assert view["progress"]["eta_seconds"] is None
        assert svc.store.get_run(run_id).state is RunState.RUNNING
        assert ProgressStore(svc.store).calibration(pilot["calibration_key"]) is None
        start = time.monotonic()
        waited = svc.wait_run(run_id, timeout_seconds=0.25, until="terminal")
        assert time.monotonic() - start < 2
        assert waited["reason"] == "timeout" and waited["timed_out"] is True
        assert waited["progress"]["run_state"] == "running"
        assert svc.store.get_run(run_id).state is RunState.RUNNING


@pytest.mark.parametrize("cached", [False, True])
def test_zero_wait_is_cache_only_without_process_or_progress_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool,
) -> None:
    with local_service(tmp_path) as svc:
        run_id = submit(svc, svc.plan_run(request(mode="heartbeat", pilot_seconds=30)))
        observed = None
        if cached:
            observed = eventually(
                lambda: svc.get_run(run_id, refresh=True),
                lambda view: view["progress"]["completed"] == 1,
            )["progress"]
        else:
            assert ProgressStore(svc.store).latest(run_id) is None
        forbidden = Mock(side_effect=AssertionError("zero wait must not issue a probe"))
        with monkeypatch.context() as patch:
            patch.setattr(svc.runner, "run", forbidden)
            patch.setattr(RunManager, "refresh", forbidden)
            patch.setattr(Collector, "read", forbidden)
            patch.setattr(svc.progress, "observe", forbidden)
            start = time.monotonic()
            waited = svc.wait_run(run_id, timeout_seconds=0, until="terminal")
            assert time.monotonic() - start < 0.5
            forbidden.assert_not_called()
        assert waited["reason"] == "timeout" and waited["timed_out"] is True
        if observed is not None:
            assert waited["progress"] == observed
            assert waited["revision"] == observed["revision"]
        else:
            assert waited["revision"] is None
            assert waited["progress"]["eta_seconds"] is None
            assert "no cached progress" in waited["progress"]["reason"]
