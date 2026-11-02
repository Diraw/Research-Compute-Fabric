"""Strict runtime decisions are based on a measured matching workload."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import PermissionDenied
from rcf.core.ids import iso, now_utc
from rcf.core.models import JobHandle, RunRecord, RunState
from rcf.progress.planning import CALIBRATION_EVIDENCE_VERSION, contract_fingerprint
from rcf.progress.store import ProgressStore
from rcf.service import RcfService


@pytest.fixture
def strict(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "train.py").write_text("print('synthetic')\n", encoding="utf-8")
    from rcf.core.runner import SubprocessRunner

    runner = SubprocessRunner()
    env = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    for args in (["init", "-q"], ["add", "."], ["-c", "commit.gpgsign=false", "commit", "-qm", "synthetic"]):
        result = runner.run(["git", *args], cwd=str(project), env=env)
        assert result.ok, result.stderr
    home = make_home(tmp_path, legacy_progress=False, nodes='[nodes.local]\nkind="local"\n',
                     projects='[projects.demo]\nentrypoint="{python} {script} --steps {steps} --out {output_dir}"\n'
                     f'script="train.py"\npaths={{local="{toml_path(project)}"}}\n')
    svc = RcfService(Config.load(home), actor="test")
    yield svc
    svc.close()


def enable(svc, *, budget=None, mode="steps", require=False):
    from rcf.progress.models import ProgressContract

    progress = ProgressContract(completion_condition="all steps completed", mode=mode,
                                total_param="steps" if mode == "steps" else None,
                                require_calibration=require)
    svc.config.projects["demo"] = svc.config.project("demo").model_copy(
        update={"progress": progress, "runtime_budget_seconds": budget})


def plan(svc, **kwargs):
    return svc.plan_run({"project_id": "demo", "params": {"steps": 100}, **kwargs})


def failures(value):
    return {c["name"] for c in value["checks"] if c["status"] == "fail"}


def test_default_unbounded_submission_without_contract_is_blocked(strict):
    value = plan(strict)
    assert "progress_contract" in failures(value)
    with pytest.raises(PermissionDenied):
        strict.submit_run(value["plan_id"], confirm=True)


def test_short_explicit_smoke_exemption_is_not_fake_eta(strict):
    value = plan(strict, requirements={"time_minutes": 1})
    assert not failures(value)
    assert value["progress_contract"] is None


def test_fixed_work_contract_injects_run_local_progress_file(strict):
    enable(strict)
    value = plan(strict)
    assert not failures(value)
    assert value["progress_total"] == 100
    assert value["command"]["env"]["RCF_PROGRESS_TOTAL"] == "100.0"
    assert value["command"]["env"]["RCF_PROGRESS_FILE"].endswith("progress.jsonl")
    assert value["command"]["env"]["RCF_PROGRESS_PILOT"] == "0"


def test_pilot_with_budget_is_explicitly_not_budget_gated(strict):
    enable(strict, budget=10)
    value = plan(strict, pilot_seconds=2, runtime_budget_seconds=1)
    assert not failures(value)
    assert any(c["name"] == "pilot_budget_ignored" and c["status"] == "warn"
               for c in value["checks"])


def test_budget_requires_measured_pilot_without_implicit_execution(strict):
    enable(strict, budget=10)
    value = plan(strict)
    assert "runtime_calibration" in failures(value)
    pilot = plan(strict, pilot_seconds=2)
    assert not failures(pilot)
    assert strict.store.get_run(pilot["run_id"]) is None
    assert pilot["command"]["env"]["RCF_PROGRESS_PILOT"] == "1"
    assert pilot["calibration_key"] == value["calibration_key"]
    assert any(c["name"] == "pilot_limit" and c["data"]["automatic_termination"] for c in pilot["checks"])


def calibration_identity(svc, value):
    stored = svc.store.get_plan(value["plan_id"])
    return {"evidence_version": CALIBRATION_EVIDENCE_VERSION,
            "total": stored.progress_total,
            "contract_fingerprint": contract_fingerprint(stored.progress_contract, stored.progress_total)}


def test_budget_stale_mismatched_and_too_slow_evidence(strict):
    enable(strict, budget=10)
    value = plan(strict)
    evidence = ProgressStore(strict.store)
    evidence.save_calibration(value["calibration_key"], "pilot", {
        **calibration_identity(strict, value),
        "run_id": "pilot", "evidence_verified": True, "evidence_at": iso(now_utc()),
        "estimated_total_seconds_upper": 20,
    })
    assert "runtime_budget" in failures(plan(strict))
    evidence.save_calibration(value["calibration_key"], "pilot", {
        **calibration_identity(strict, value),
        "run_id": "pilot", "evidence_verified": True,
        "evidence_at": iso(now_utc() - timedelta(days=20)), "estimated_total_seconds_upper": 2,
    })
    assert "runtime_calibration" in failures(plan(strict))


def test_request_budget_cannot_exceed_project_policy(strict):
    enable(strict, budget=10)
    value = plan(strict, runtime_budget_seconds=100)
    assert "runtime_budget_policy" in failures(value)


def test_budget_without_contract_cannot_use_smoke_exemption(strict):
    strict.config.projects["demo"] = strict.config.project("demo").model_copy(
        update={"runtime_budget_seconds": 600})
    value = plan(strict, requirements={"time_minutes": 1})
    assert "progress_contract" in failures(value)


def test_calibration_without_evidence_timestamp_is_not_trusted(strict):
    enable(strict, budget=10)
    value = plan(strict)
    ProgressStore(strict.store).save_calibration(value["calibration_key"], "pilot", {
        **calibration_identity(strict, value),
        "run_id": "pilot", "evidence_verified": True, "observed_at": iso(now_utc()),
        "estimated_total_seconds_upper": 2,
    })
    assert "runtime_calibration" in failures(plan(strict))


def test_budget_or_contract_policy_change_keeps_workload_identity(strict):
    enable(strict, budget=10)
    first = plan(strict)
    enable(strict, budget=20, require=True)
    second = plan(strict)
    assert first["calibration_key"] == second["calibration_key"]


def test_observation_threshold_changes_keep_measured_workload_identity(strict):
    enable(strict, budget=500)
    value = plan(strict)
    _terminal_history(strict, value, days_ago=0.001)
    strict.get_run_progress(value["run_id"], refresh_state=False)
    strict.config.projects["demo"].progress = strict.config.project("demo").progress.model_copy(
        update={"heartbeat_timeout_seconds": 300, "stall_timeout_seconds": 600,
                "start_grace_seconds": 120, "require_calibration": True})
    changed = plan(strict)
    assert changed["calibration_key"] == value["calibration_key"]
    assert not failures(changed)


def test_workload_change_never_reuses_smaller_grid_calibration(strict):
    enable(strict, budget=10)
    first = plan(strict)
    other = strict.plan_run({"project_id": "demo", "params": {"steps": 200}})
    assert first["calibration_key"] != other["calibration_key"]


def _terminal_history(svc, value, *, days_ago=30):
    run_dir = svc.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    end = now_utc() - timedelta(days=days_ago)
    start = end - timedelta(seconds=10)
    records = [
        {"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
         "timestamp": iso(start + timedelta(seconds=offset)), "elapsed_seconds": offset,
         "phase": "compute", "completed": float(completed), "total": 100.0,
         "metric": None, "eta_reason": None, "finished": index == 4}
        for index, (completed, offset) in enumerate([(0, 0.0), (25, 2.5), (50, 5.0),
                                                    (75, 7.5), (100, 10.0)])
    ]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    svc.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                   node_id="local", executor="local", state=RunState.SUCCEEDED,
                                   created_at=start, submitted_at=start, finished_at=end, exit_code=0,
                                   handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    return end


@pytest.mark.parametrize("change", [
    {"total": 1_000_000},
    {"completion_condition": "one step means one full epoch, not one batch"},
    {"mode": "phases", "phase_weights": {"compute": 1.0}},
    {"source": "log_json"},
])
def test_semantic_contract_changes_invalidate_observed_calibration(strict, change):
    from rcf.progress.models import ProgressContract

    enable(strict, budget=500)
    original = ProgressContract(total=100, completion_condition="all steps completed")
    strict.config.projects["demo"].progress = original
    value = plan(strict)
    _terminal_history(strict, value, days_ago=0.001)
    strict.get_run_progress(value["run_id"], refresh_state=False)
    assert not failures(plan(strict))
    strict.config.projects["demo"].progress = original.model_copy(update=change)
    changed = plan(strict)
    assert changed["calibration_key"] != value["calibration_key"]
    assert "runtime_calibration" in failures(changed)


def test_legacy_or_mismatched_payload_is_rejected_even_under_current_key(strict):
    enable(strict, budget=500)
    value = plan(strict)
    _terminal_history(strict, value, days_ago=0.001)
    strict.get_run_progress(value["run_id"], refresh_state=False)
    evidence = ProgressStore(strict.store)
    measured = evidence.calibration(value["calibration_key"])
    assert not failures(plan(strict))
    for update in ({"total": 1_000_000}, {"evidence_version": 1},
                   {"evidence_version": None}, {"contract_fingerprint": "other-semantic-contract"}):
        evidence.save_calibration(value["calibration_key"], value["run_id"], {**measured, **update})
        assert "runtime_calibration" in failures(plan(strict)), update


def test_heartbeat_wait_is_charged_to_advance_checkpoint_calibration(strict):
    enable(strict, budget=500)
    value = plan(strict, pilot_seconds=300)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    end = now_utc() - timedelta(seconds=1)
    start = end - timedelta(seconds=300)
    checkpoints = [(0, 0), (100, 0), (101, 1), (298, 1), (299, 2)]
    records = [
        {"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
         "timestamp": iso(start + timedelta(seconds=elapsed)), "elapsed_seconds": elapsed,
         "phase": "compute", "completed": completed, "total": 100.0,
         "metric": None, "eta_reason": None, "finished": False}
        for index, (elapsed, completed) in enumerate(checkpoints)
    ]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(
        run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
        node_id="local", executor="local", state=RunState.FAILED,
        created_at=start, submitted_at=start, finished_at=end, exit_code=124,
        handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    strict.get_run_progress(value["run_id"], refresh_state=False)
    measured = ProgressStore(strict.store).calibration(value["calibration_key"])
    assert measured is not None
    assert measured["rate_per_second"] < 0.02
    assert measured["estimated_total_seconds_upper"] >= 19_703
    assert "runtime_budget" in failures(plan(strict))


def test_long_log_tail_keeps_rolling_eta_past_sample_limit(strict):
    enable(strict)
    value = plan(strict)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    records = [{"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
                "timestamp": iso(now - timedelta(seconds=300 - index)), "elapsed_seconds": float(index),
                "phase": "compute", "completed": float(index), "total": 1000.0,
                "metric": None, "eta_reason": None, "finished": False} for index in range(300)]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING,
                                     created_at=now - timedelta(seconds=300),
                                     submitted_at=now - timedelta(seconds=300),
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["eta_seconds"] is not None
    assert not [error for error in observed["evidence_errors"] if "sample limit" not in error]


def test_delayed_reporter_start_never_resets_budget_clock(strict):
    enable(strict, budget=100)
    value = plan(strict)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    records = [{"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
                "timestamp": iso(now - timedelta(seconds=3 - index)), "elapsed_seconds": float(index),
                "phase": "compute", "completed": float(index * 5), "total": 100.0,
                "metric": None, "eta_reason": None, "finished": False} for index in range(3)]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING,
                                     created_at=now - timedelta(seconds=600),
                                     submitted_at=now - timedelta(seconds=600),
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["elapsed_seconds"] >= 590
    assert "budget_exceeded" in {alert.get("code") for alert in observed["alerts"]}
    assert strict.store.get_run(value["run_id"]).state is RunState.RUNNING


def test_old_terminal_history_does_not_refresh_expired_calibration(strict):
    enable(strict, budget=10)
    value = plan(strict)
    _terminal_history(strict, value)
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["run_state"] == "succeeded"
    calibration = ProgressStore(strict.store).calibration(value["calibration_key"])
    assert calibration is not None and calibration["evidence_at"].startswith(
        iso(now_utc() - timedelta(days=30))[:10])
    assert "runtime_calibration" in failures(plan(strict))


def test_old_history_cannot_overwrite_newer_measured_calibration(strict):
    enable(strict, budget=10)
    value = plan(strict)
    ProgressStore(strict.store).save_calibration(value["calibration_key"], "pilot-new", {
        **calibration_identity(strict, value),
        "run_id": "pilot-new", "evidence_verified": True, "evidence_at": iso(now_utc()),
        "estimated_total_seconds_upper": 2,
    })
    _terminal_history(strict, value)
    strict.get_run_progress(value["run_id"], refresh_state=False)
    assert ProgressStore(strict.store).calibration(value["calibration_key"])["run_id"] == "pilot-new"
    assert not failures(plan(strict))


def test_executor_started_at_excludes_queue_time_from_budget_clock(strict):
    enable(strict, budget=10000)
    value = plan(strict, runtime_budget_seconds=10000)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    submitted = now - timedelta(seconds=1000)
    started = now - timedelta(seconds=600)
    (run_dir / "started_at").write_text(iso(started), encoding="utf-8")
    records = [
        {"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
         "timestamp": iso(started + timedelta(seconds=index * 100)),
         "elapsed_seconds": float(index * 100), "phase": "compute",
         "completed": float(index * 50), "total": 200.0, "metric": None,
         "eta_reason": None, "finished": False}
        for index in range(3)
    ]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING,
                                     created_at=submitted, submitted_at=submitted,
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert 590 <= observed["elapsed_seconds"] <= 610
    assert "budget_exceeded" not in {alert.get("code") for alert in observed["alerts"]}


def test_pilot_timeout_is_explicit_in_progress_view(strict):
    enable(strict)
    value = plan(strict, pilot_seconds=2)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.FAILED,
                                     created_at=now - timedelta(seconds=3),
                                     submitted_at=now - timedelta(seconds=3),
                                     finished_at=now, exit_code=124,
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["pilot_timed_out"] is True
    assert "pilot_timeout" in {alert.get("code") for alert in observed["alerts"]}
    assert "exit 124" in observed["reason"]


def test_terminal_runtime_freezes_at_controller_finished_at(strict):
    enable(strict)
    value = plan(strict)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    started = now - timedelta(seconds=100)
    finished = now - timedelta(seconds=40)
    (run_dir / "started_at").write_text(iso(started), encoding="utf-8")
    (run_dir / "finished_at").write_text(iso(finished), encoding="utf-8")
    records = [
        {"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
         "timestamp": iso(started + timedelta(seconds=index * 3)),
         "elapsed_seconds": float(index * 3), "phase": "compute",
         "completed": float(index * 50), "total": 200.0, "metric": None,
         "eta_reason": None, "finished": index == 2}
        for index in range(3)
    ]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.SUCCEEDED,
                                     created_at=started - timedelta(seconds=10),
                                     submitted_at=started - timedelta(seconds=10),
                                     finished_at=now, exit_code=0,
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert 55 <= observed["elapsed_seconds"] <= 65
    assert observed["run_state"] == "succeeded"


def test_clock_offset_survives_reporter_restart_and_tail_truncation(strict):
    enable(strict)
    value = plan(strict)
    run_id = value["run_id"]
    ProgressStore(strict.store).save_observation(run_id, {
        "run_id": run_id, "clock_offset_seconds": 40, "status": "progressing",
        "run_state": "running", "alerts": [],
    }, "prior-revision")
    run_dir = strict.config.home / "runs" / run_id
    run_dir.mkdir(parents=True)
    now = now_utc()
    submitted = now - timedelta(seconds=10)
    records = [
        {"run_id": run_id, "session_id": "restarted", "seq": index + 1,
         "timestamp": iso(now - timedelta(seconds=48) + timedelta(seconds=index * 2)),
         "elapsed_seconds": float(index * 2), "phase": "compute",
         "completed": float(index * 25), "total": 100.0, "metric": None,
         "eta_reason": None, "finished": False}
        for index in range(3)
    ]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=run_id, plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING,
                                     created_at=submitted, submitted_at=submitted,
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(run_id, refresh_state=False)
    assert observed["clock_offset_seconds"] == 40
    assert observed["last_report_age_seconds"] < 10
    assert "stale_progress" not in {alert.get("code") for alert in observed["alerts"]}


def test_worker_clock_behind_controller_normalizes_ages_without_fake_stale(strict):
    enable(strict)
    value = plan(strict)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    submitted = now - timedelta(seconds=100)
    records = [
        {"run_id": value["run_id"], "session_id": "worker", "seq": index + 1,
         "timestamp": iso(submitted - timedelta(seconds=130) + timedelta(seconds=index * 50)),
         "elapsed_seconds": float(index * 50), "phase": "compute",
         "completed": float(index * 50), "total": 200.0, "metric": None,
         "eta_reason": None, "finished": False}
        for index in range(3)
    ]
    (run_dir / "progress.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING,
                                     created_at=submitted, submitted_at=submitted,
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["eta_seconds"] is not None
    assert observed["last_report_age_seconds"] < 6
    assert observed["clock_offset_seconds"] == 130
    assert "clock_skew" in {alert.get("code") for alert in observed["alerts"]}


def test_running_contract_without_reports_alerts_missing_evidence(strict):
    enable(strict)
    value = plan(strict)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING,
                                     created_at=now - timedelta(minutes=10),
                                     submitted_at=now - timedelta(minutes=10),
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["eta_seconds"] is None
    assert "progress_missing" in {alert.get("code") for alert in observed["alerts"]}


def test_progress_heartbeat_does_not_create_eta_without_advancement(strict):
    enable(strict)
    value = plan(strict)
    run_dir = strict.config.home / "runs" / value["run_id"]
    run_dir.mkdir(parents=True)
    now = now_utc()
    records = [{"run_id": value["run_id"], "session_id": "worker", "seq": i + 1,
                "timestamp": iso(now - timedelta(seconds=30-i*10)), "elapsed_seconds": float(i*10),
                "phase": "compute", "completed": 1.0, "total": 100.0, "metric": None,
                "eta_reason": None, "finished": False} for i in range(3)]
    (run_dir / "progress.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records), encoding="utf-8")
    strict.store.insert_run(RunRecord(run_id=value["run_id"], plan_id=value["plan_id"], project_id="demo",
                                     node_id="local", executor="local", state=RunState.RUNNING, created_at=now,
                                     submitted_at=now-timedelta(seconds=30),
                                     handle=JobHandle(kind="local", node_id="local", workdir=str(run_dir))))
    observed = strict.get_run_progress(value["run_id"], refresh_state=False)
    assert observed["eta_seconds"] is None
    assert observed["run_state"] == "running"
    assert strict.store.get_run(value["run_id"]).state is RunState.RUNNING
    cached = strict.wait_run(value["run_id"], timeout_seconds=0)
    assert cached["reason"] == "timeout"
    assert cached["progress"]["revision"] == observed["revision"]
