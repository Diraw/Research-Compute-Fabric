"""No side-effect trial runs: plan checks consume only verified runtime evidence."""

from __future__ import annotations

import math
from datetime import timedelta

from ..core.errors import Conflict
from ..core.ids import now_utc, parse_iso, stable_hash
from ..core.models import Check, ProbeStatus
from .store import ProgressStore


def requires_contract(config, request) -> bool:
    minutes = request.requirements.time_minutes
    project = config.projects.get(request.project_id)
    budget = request.runtime_budget_seconds or (project.runtime_budget_seconds if project else None)
    if budget is not None:
        return True
    return bool(config.settings.progress.enforce_long_runs and
                (request.pilot_seconds is not None or minutes is None or
                 minutes > config.settings.progress.short_run_minutes))


# Changing the calibration algorithm invalidates old over-optimistic evidence,
# even if an older payload is copied under a newly computed workload key.
CALIBRATION_EVIDENCE_VERSION = 2


def contract_fingerprint(contract, total) -> str:
    """Only computational semantics, not heartbeat or budget policy."""
    return stable_hash({
        "source": contract.source if contract else None,
        "mode": contract.mode if contract else None,
        "completion_condition": contract.completion_condition if contract else None,
        "phase_weights": contract.phase_weights if contract else None,
        "total_param": contract.total_param if contract else None,
        "total": total,
    }, length=64)


def calibration_matches(calibration, contract, total) -> bool:
    return bool(calibration and calibration.get("evidence_verified") is True
                and calibration.get("evidence_version") == CALIBRATION_EVIDENCE_VERSION
                and calibration.get("total") == total
                and calibration.get("contract_fingerprint") == contract_fingerprint(contract, total))


def workload_key(config, plan) -> str:
    project = config.project(plan.request.project_id)
    requirements = plan.request.requirements.model_dump(mode="json")
    # A Slurm walltime is a limit, not workload identity or measured ETA.
    requirements.pop("time_minutes", None)
    # Calibration describes the computation, not retention/budget policy: changing
    # a budget or progress threshold must never require a fresh pilot by itself.
    project_identity = project.model_dump(mode="json")
    for policy_key in ("progress", "runtime_budget_seconds", "storage_budget_bytes",
                       "storage_budget_files", "storage_keep_recent_runs", "disposable_globs",
                       "sync_exclude", "sync_include", "name", "description", "notes"):
        project_identity.pop(policy_key, None)
    try:
        total = resolved_total(project, plan.request)
    except ValueError:
        total = "invalid"  # checks_for_plan returns the structured contract failure.
    return stable_hash({
        "evidence_version": CALIBRATION_EVIDENCE_VERSION,
        "contract_fingerprint": contract_fingerprint(project.progress, total),
        "project": project_identity, "node": config.node(plan.node_id).model_dump(mode="json"),
        "params": plan.request.params, "requirements": requirements, "override": plan.request.command_override,
        "env_id": plan.env_id, "env_fingerprint": plan.env_fingerprint,
        "git_commit": plan.git_commit, "git_dirty": plan.git_dirty,
        "inputs": plan.inputs.model_dump(mode="json") if plan.inputs else None,
    }, length=64)


def resolved_total(project, request) -> float | None:
    contract = project.progress
    if contract is None:
        return None
    raw = request.params.get(contract.total_param) if contract.total_param else contract.total
    if raw is None and contract.mode != "steps":
        return None
    if isinstance(raw, bool):
        raise ValueError("progress total cannot be boolean")
    try:
        value = float(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("progress total/total_param must resolve to a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("progress total must be finite and positive")
    return value


def checks_for_plan(config, store, plan) -> list[Check]:
    project = config.project(plan.request.project_id)
    contract = project.progress
    checks = []
    if contract is None:
        checks.append(Check(name="progress_contract", status="fail" if requires_contract(config, plan.request) else "warn",
                            detail="long/unbounded work requires a progress/completion contract; explicit <=5 minute smoke runs may be exempt"))
        return checks
    try:
        total = resolved_total(project, plan.request)
    except ValueError as exc:
        return [Check(name="progress_contract", status="fail", detail=str(exc))]
    checks.append(Check(name="progress_contract", status="ok", detail=contract.completion_condition,
                        data={"source": contract.source, "mode": contract.mode, "total": total}))
    if plan.request.pilot_seconds is not None:
        checks.append(Check(name="pilot_limit", status="ok",
                            detail=f"explicit bounded pilot: watchdog terminates after {plan.request.pilot_seconds}s; a full workload is not silently submitted",
                            data={"seconds": plan.request.pilot_seconds, "automatic_termination": True}))
        if plan.request.runtime_budget_seconds is not None:
            checks.append(Check(name="pilot_budget_ignored", status="warn",
                                detail="runtime_budget_seconds is evaluated on the formal plan; a bounded pilot is not budget-gated"))
        return checks
    request_budget = plan.request.runtime_budget_seconds
    project_budget = project.runtime_budget_seconds
    if request_budget is not None and project_budget is not None and request_budget > project_budget:
        checks.append(Check(name="runtime_budget_policy", status="fail",
                            detail=f"requested budget {request_budget:.1f}s exceeds the project policy {project_budget:.1f}s"))
        return checks
    budget = request_budget or project_budget
    required = contract.require_calibration or budget is not None
    calibration = ProgressStore(store).calibration(plan.calibration_key)
    if required and (not plan.git_commit or plan.git_dirty):
        checks.append(Check(name="calibration_code", status="fail",
                            detail="budgeted/calibrated formal runs require a clean committed checkout; dirty/no-git code is not an immutable workload identity"))
    if not calibration_matches(calibration, contract, total):
        checks.append(Check(name="runtime_calibration", status="fail" if required else "warn",
                            detail="no matching measured calibration; plan and confirm a bounded pilot with the same workload first"))
        return checks
    stamp = parse_iso(calibration.get("evidence_at"))
    fresh = stamp is not None and stamp + timedelta(hours=config.settings.progress.calibration_max_age_hours) >= now_utc()
    if not fresh:
        checks.append(Check(name="runtime_calibration", status="fail" if required else "warn",
                            detail="matching calibration is stale; rerun an explicit pilot"))
        return checks
    upper = calibration.get("estimated_total_seconds_upper")
    if (isinstance(upper, bool) or not isinstance(upper, (int, float))
            or not math.isfinite(upper) or upper <= 0):
        checks.append(Check(name="runtime_calibration", status="fail" if required else "warn",
                            detail="calibration has no credible whole-run duration; convergence/multiphase needs evidence, not a guessed number"))
        return checks
    checks.append(Check(name="runtime_calibration", status="ok", detail="matching measured workload calibration",
                        data={"source_run_id": calibration.get("run_id"), "estimated_total_seconds_upper": upper}))
    if budget is not None:
        checks.append(Check(name="runtime_budget", status="fail" if upper > budget else "ok",
                            detail=f"measured conservative estimate {upper:.1f}s vs budget {budget:.1f}s; walltime is not ETA",
                            data={"estimate_upper_seconds": upper, "budget_seconds": budget}))
    return checks


def attach_contract(config, store, plan) -> None:
    project = config.project(plan.request.project_id)
    plan.progress_contract = project.progress
    # A short smoke exemption must be enforced on SSH/Local too, not merely a
    # declared number that only Slurm understands. Formal budgets remain warn-only.
    if plan.request.pilot_seconds:
        plan.execution_timeout_seconds = plan.request.pilot_seconds
    elif (project.progress is None and plan.request.requirements.time_minutes is not None
          and plan.request.requirements.time_minutes <= config.settings.progress.short_run_minutes
          and config.node(plan.node_id).kind.value != "slurm"):
        # Slurm already enforces --time; direct SSH/local smoke runs need the
        # watchdog because the declared limit is otherwise advisory only.
        plan.execution_timeout_seconds = plan.request.requirements.time_minutes * 60
    plan.calibration_key = workload_key(config, plan)
    try:
        plan.progress_total = resolved_total(project, plan.request)
    except ValueError:
        plan.progress_total = None
    # Never report the scheduler's requested time limit as a measured ETA.
    plan.checks.extend(checks_for_plan(config, store, plan))
    if plan.execution_timeout_seconds and not plan.request.pilot_seconds:
        plan.checks.append(Check(name="smoke_limit", status="warn",
                                 detail=f"legacy short-smoke exemption: enforced hard timeout {plan.execution_timeout_seconds}s, not an ETA"))


def validate_submission(config, store, plan) -> None:
    if plan.calibration_key is None and not requires_contract(config, plan.request):
        return
    project = config.project(plan.request.project_id)
    if project.progress != plan.progress_contract:
        raise Conflict("progress contract changed; create and review a fresh plan")
    if plan.calibration_key is None:
        if requires_contract(config, plan.request):
            raise Conflict("legacy plan has no progress evidence; create a fresh plan")
        return
    if workload_key(config, plan) != plan.calibration_key:
        raise Conflict("workload configuration changed; calibration cannot be reused")
    blockers = [c for c in checks_for_plan(config, store, plan) if c.status is ProbeStatus.FAIL]
    if blockers:
        raise Conflict("runtime contract/calibration no longer permits dispatch",
                       detail={"checks": [c.model_dump(mode="json") for c in blockers]})
