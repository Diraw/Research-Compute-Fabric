"""Progress observations, measured calibrations and bounded change waiting."""

from __future__ import annotations

import math
import time
from datetime import timedelta

from ..core.errors import Conflict, NotFound, SpecError
from ..core.ids import iso, now_utc, parse_iso, stable_hash
from .collector import Collector
from .estimator import estimate
from .planning import (
    CALIBRATION_EVIDENCE_VERSION,
    calibration_matches,
    contract_fingerprint,
    workload_key,
)
from .store import ProgressStore


class DeadlineRunner:
    """One absolute deadline shared by every subprocess in a wait observation."""

    def __init__(self, runner, deadline):
        self.runner, self.deadline = runner, deadline

    def run(self, argv, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Conflict("bounded wait observation deadline reached")
        kwargs["timeout"] = min(kwargs.get("timeout") or remaining, remaining)
        return self.runner.run(argv, **kwargs)


class ProgressManager:
    def __init__(self, config, store, runner, runs):
        self.config, self.store, self.runner, self.runs = config, store, runner, runs
        self.evidence = ProgressStore(store)

    def observe(self, run_id, *, deadline=None):
        record = self.store.get_run(run_id)
        if record is None:
            raise NotFound("unknown run", detail={"run_id": run_id})
        plan = self.store.get_plan(record.plan_id) if record.plan_id else None
        contract = plan.progress_contract if plan else None
        samples, errors = [], []
        evidence_complete = False
        truncated = False
        execution_started_at = None
        execution_finished_at = None
        if contract and record.handle and record.handle.workdir:
            seconds = self.config.settings.progress.collect_timeout_seconds
            if deadline is not None:
                seconds = min(seconds, max(0.05, deadline - time.monotonic()))
            runner = DeadlineRunner(self.runner, deadline) if deadline is not None else self.runner
            try:
                evidence = Collector(runner).read(
                    self.config.node(record.node_id), record.handle.workdir, run_id,
                    source=contract.source, timeout_seconds=seconds,
                )
                samples, errors = evidence.get("samples", []), evidence.get("errors", [])
                evidence_complete = bool(evidence.get("complete"))
                truncated = bool(evidence.get("truncated"))
                execution_started_at = evidence.get("started_at") or None
                execution_finished_at = evidence.get("finished_at") or None
            except Exception as exc:
                errors = [str(exc)[:400]]
        elif not contract:
            errors = ["project/run has no progress contract; ETA cannot be inferred from walltime"]
        elif not record.handle:
            errors = ["job has no submission handle; progress has not started"]
        previous_observation = self.evidence.latest(run_id)
        if samples and truncated and previous_observation:
            old_marker = previous_observation.get("advance_marker")
            old_first = samples[0]
            if (old_marker and old_marker.get("session_id") == old_first.get("session_id")
                    and old_marker.get("phase") == old_first.get("phase")
                    and old_marker.get("completed") <= old_first.get("completed")
                    and old_marker.get("seq", 0) < old_first.get("seq", 0)):
                samples = [old_marker, *samples]
        baseline = self.evidence.calibration(plan.calibration_key) if plan and plan.calibration_key else None
        if plan and not calibration_matches(baseline, contract, plan.progress_total):
            baseline = None
        budget = plan.request.runtime_budget_seconds if plan else None
        if budget is None and record.project_id in self.config.projects:
            budget = self.config.project(record.project_id).runtime_budget_seconds
        estimate_now = now_utc()
        clock_offset = 0.0
        first_report = parse_iso(samples[0]["timestamp"]) if samples else None
        if first_report and samples:
            # Subtract session-local elapsed so a bounded tail still reveals
            # where this worker session began, not merely when it was retained.
            first_report -= timedelta(seconds=samples[0]["elapsed_seconds"])
        if first_report and record.submitted_at and first_report < record.submitted_at:
            behind = (record.submitted_at - first_report).total_seconds()
            if behind > 5:
                # A worker cannot report before the controller submitted it.
                clock_offset = behind
        if previous_observation:
            # After a reporter restart the bounded tail starts a new session;
            # its first sample estimates a *smaller* offset because the restart
            # happened after submission. Keep the larger earlier offset unless
            # the latest sample proves it would be in the future.
            prior = previous_observation.get("clock_offset_seconds")
            if isinstance(prior, (int, float)) and prior > 5:
                last_sample_at = parse_iso(samples[-1]["timestamp"]) if samples else None
                if last_sample_at is None:
                    clock_offset = max(clock_offset, float(prior))
                else:
                    age_with_prior = (
                        now_utc() - timedelta(seconds=float(prior)) - last_sample_at
                    ).total_seconds()
                    if age_with_prior >= -5:
                        clock_offset = max(clock_offset, float(prior))
        if record.state.terminal:
            terminal_at = parse_iso(execution_finished_at) if execution_finished_at else None
            if terminal_at is not None:
                terminal_at += timedelta(seconds=clock_offset)
                if terminal_at <= now_utc() + timedelta(seconds=5):
                    estimate_now = terminal_at
            elif record.finished_at is not None:
                # Older executors did not leave a finished_at file.
                estimate_now = record.finished_at
            if samples:
                last_sample_at = parse_iso(samples[-1]["timestamp"])
                if last_sample_at is not None:
                    last_sample_at += timedelta(seconds=clock_offset)
                    if last_sample_at > estimate_now:
                        # A terminal observation can be a hair older than the
                        # last report; never turn polling order into skew.
                        estimate_now = last_sample_at
        started_at = parse_iso(execution_started_at) if execution_started_at else None
        if started_at is not None:
            # The executor writes started_at on the node clock. Shift it into
            # controller time before using it as the budget clock.
            started_at += timedelta(seconds=clock_offset)
            if started_at > estimate_now + timedelta(seconds=5):
                started_at = None
        if started_at is None and self.config.node(record.node_id).kind.value != "slurm":
            started_at = record.submitted_at
        kwargs = {
            "now": estimate_now, "started_at": started_at, "age_offset_seconds": clock_offset,
            "state": record.state.value, "budget_seconds": budget,
            "baseline_rate": baseline.get("rate_per_second") if baseline else None,
        }
        if contract:
            kwargs.update(mode=contract.mode, phase_weights=contract.phase_weights or None,
                          heartbeat_timeout_seconds=contract.heartbeat_timeout_seconds,
                          stall_timeout_seconds=contract.stall_timeout_seconds,
                          start_grace_seconds=contract.start_grace_seconds)
        view = estimate(samples, **kwargs)
        advance_marker = None
        for sample in samples:
            if (advance_marker is None or sample["session_id"] != advance_marker["session_id"]
                    or sample["phase"] != advance_marker["phase"]
                    or sample["completed"] != advance_marker["completed"]):
                advance_marker = sample
        view["advance_marker"] = advance_marker
        if clock_offset:
            view["clock_offset_seconds"] = clock_offset
            view.setdefault("alerts", []).append({
                "code": "clock_skew",
                "message": f"Worker clock is {clock_offset:.0f}s behind the controller; report ages were normalized."})
        view.update(run_id=run_id, run_state=record.state.value, observed_at=iso(now_utc()),
                    evidence_errors=errors, evidence_complete=evidence_complete,
                    completion_condition=contract.completion_condition if contract else None,
                    runtime_budget_seconds=budget, pilot_seconds=plan.request.pilot_seconds if plan else None)
        if (contract is not None and not samples and record.state.value == "running"
                and not [error for error in errors if "does not exist" not in error]):
            reference = record.submitted_at
            age = (now_utc() - reference).total_seconds() if reference else None
            if age is None or age > contract.start_grace_seconds:
                view.setdefault("alerts", []).append({
                    "code": "progress_missing",
                    "message": "Run is running but no progress evidence was observed; check the reporter path and RCF_PROGRESS_FILE."})
        if contract is None and not record.state.terminal:
            view.update(eta_seconds=None, eta_range_seconds=None, eta_scope="unavailable",
                        confidence="none", reason="No progress contract; walltime is not an ETA.")
            view.setdefault("alerts", []).append({
                "code": "progress_contract_missing",
                "message": errors[0] if errors else "Project/run has no progress contract."})
        if (plan and plan.request.pilot_seconds is not None
                and record.state.value == "failed" and record.exit_code == 124):
            view["pilot_timed_out"] = True
            view["reason"] = "Bounded pilot watchdog reached its deadline (exit 124); no formal run was submitted."
            view.setdefault("alerts", []).append({
                "code": "pilot_timeout",
                "message": f"Pilot limit {plan.request.pilot_seconds}s was enforced by the watchdog."})
        if truncated and not previous_observation and samples and len({s["completed"] for s in samples}) == 1:
            view.update(eta_seconds=None, eta_range_seconds=None, confidence="none", eta_scope="unavailable")
            view.setdefault("alerts", []).append({"code": "advance_history_unknown",
                                                   "message": "bounded tail contains only heartbeats; last real advance predates retained evidence"})
        fatal_errors = [error for error in errors if not any(marker in error for marker in (
            "byte limit:", "sample limit:", "partial trailing progress line ignored",
            "has no progress contract", "has no submission handle",
            "does not exist"))]
        if fatal_errors:
            view.setdefault("alerts", []).append({"code": "progress_evidence_invalid", "message": "; ".join(fatal_errors)})
            view.update(eta_seconds=None, eta_range_seconds=None, confidence="none",
                        eta_scope="unavailable", reason="; ".join(errors))
        # Meaningful revisions do not change every heartbeat/second of ETA.
        fraction = view.get("fraction")
        bucket = int(fraction * 100) if isinstance(fraction, (int, float)) and math.isfinite(fraction) else None
        revision = stable_hash({"state": record.state.value, "phase": view.get("phase"),
                                "milestone": bucket, "status": view.get("status"),
                                "eta_available": view.get("eta_seconds") is not None,
                                "alerts": sorted(a.get("code", "") for a in view.get("alerts", [])),
                                "errors": errors}, length=24)
        result = self.evidence.save_observation(run_id, view, revision)
        if plan and record.state.terminal and samples and evidence_complete and not errors and not truncated:
            self._calibrate(record, plan, samples, result)
        return result

    def _calibrate(self, record, plan, samples, view):
        """Only terminal, real advancement with matching declared total is reusable."""
        if not plan.calibration_key or not plan.progress_contract:
            return
        if plan.calibration_key != workload_key(self.config, plan):
            return  # Legacy algorithm/changed semantics must not mint fresh trusted evidence.
        if record.state.value not in {"succeeded", "failed"}:
            return
        if record.state.value == "failed" and not (plan.request.pilot_seconds and record.exit_code == 124):
            return
        # A pilot must actually consume a representative duration or complete
        # its declared work; an early crash is never a timing calibration.
        if (plan.request.pilot_seconds and record.state.value == "failed"
                and max(s.get("elapsed_seconds", 0) for s in samples) < plan.request.pilot_seconds * 0.5):
            return
        if plan.progress_contract.mode != "steps" or not plan.progress_total:
            return
        phase = samples[-1].get("phase")
        session = samples[-1].get("session_id")
        segment = [s for s in samples if s.get("session_id") == session and s.get("phase") == phase]
        if (len(segment) < 3 or len({s.get("phase") for s in samples}) != 1
                or len({s.get("session_id") for s in samples}) != 1):
            return
        if any(sample.get("total") != plan.progress_total for sample in segment):
            return
        rates = []
        checkpoint = segment[0]
        for first, second in zip(segment, segment[1:], strict=False):
            delta = second["completed"] - first["completed"]
            seconds = second["elapsed_seconds"] - first["elapsed_seconds"]
            if delta < 0 or seconds < 0:
                return
            if second["completed"] > checkpoint["completed"]:
                duration = second["elapsed_seconds"] - checkpoint["elapsed_seconds"]
                if duration <= 0:
                    return
                # Heartbeats do not reset the rate clock: all waiting since
                # the last actual advance belongs to the next unit of work.
                rates.append((second["completed"] - checkpoint["completed"]) / duration)
                checkpoint = second
        if len(rates) < 2:
            return
        first_advance = next((s["elapsed_seconds"] for s in segment if s["completed"] > 0), None)
        if first_advance is None or first_advance > max(5.0, segment[-1]["elapsed_seconds"] * 0.8):
            return
        low_rate = min(rates)
        if low_rate <= 0 or max(rates) / low_rate > 4:
            return  # Unstable/warmup-heavy sample; no false confidence.
        elapsed = segment[-1]["elapsed_seconds"]
        finished = segment[-1]["completed"]
        upper = elapsed + max(0, plan.progress_total - finished) / low_rate
        # Account for observed supervisor/startup overhead, never queue time as ETA.
        if record.finished_at and record.submitted_at and self.config.node(record.node_id).kind.value != "slurm":
            overhead = max(0, (record.finished_at - record.submitted_at).total_seconds() - elapsed)
            upper += overhead
        if record.finished_at is None or record.finished_at > now_utc():
            return
        previous = self.evidence.calibration(plan.calibration_key)
        if calibration_matches(previous, plan.progress_contract, plan.progress_total):
            previous_stamp = parse_iso(previous.get("evidence_at"))
            if previous_stamp is not None and previous_stamp >= record.finished_at:
                return  # Re-reading history never renews evidence or overwrites newer measurements.
        payload = {"run_id": record.run_id, "evidence_verified": True,
                   "evidence_version": CALIBRATION_EVIDENCE_VERSION,
                   "contract_fingerprint": contract_fingerprint(plan.progress_contract, plan.progress_total),
                   "evidence_at": iso(record.finished_at), "collected_at": iso(now_utc()),
                   "observed_at": iso(record.finished_at),
                   "rate_per_second": sum(rates) / len(rates), "estimated_total_seconds_upper": upper,
                   "samples": len(segment), "total": plan.progress_total,
                   "pilot_seconds": plan.request.pilot_seconds, "phase": phase,
                   "reason": "matching fixed-work terminal observation; estimate, not a guarantee"}
        self.evidence.save_calibration(plan.calibration_key, record.run_id, payload)

    def wait(self, run_id, *, timeout_seconds=30, after_revision=None, until="change"):
        limit = min(60, self.config.settings.progress.max_wait_seconds)
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= limit):
            raise SpecError(f"wait timeout must be between 0 and {limit} seconds")
        if until not in {"change", "terminal", "phase", "alert"}:
            raise SpecError("wait condition must be change, terminal, phase or alert")
        deadline = time.monotonic() + timeout_seconds
        initial = self.evidence.latest(run_id)
        initial_phase = initial.get("phase") if initial else None
        initial_alerts = {a.get("code") for a in initial.get("alerts", [])} if initial else set()
        cursor = after_revision or (initial.get("revision") if initial else None)
        latest = initial
        while True:
            if time.monotonic() < deadline:
                from ..runs.manager import RunManager

                bounded = RunManager(self.config, self.store, self.runs.planner, DeadlineRunner(self.runner, deadline))
                bounded.refresh(run_id)
                if time.monotonic() < deadline:
                    latest = self.observe(run_id, deadline=deadline)
            if latest is None:
                # Zero-duration reads are cache-only and never spawn a remote probe.
                record = self.store.get_run(run_id)
                if record is None:
                    raise NotFound("unknown run")
                latest = {"run_id": run_id, "run_state": record.state.value, "revision": None,
                          "eta_seconds": None, "reason": "no cached progress; bounded wait elapsed"}
            terminal = latest["run_state"] in {"succeeded", "failed", "cancelled", "lost"}
            changed = cursor is not None and latest.get("revision") != cursor
            if cursor is None:
                cursor, initial_phase = latest.get("revision"), latest.get("phase")
                initial_alerts = {a.get("code") for a in latest.get("alerts", [])}
            reached = terminal or (until == "change" and changed) or (
                until == "phase" and latest.get("phase") != initial_phase) or (
                until == "alert" and bool({a.get("code") for a in latest.get("alerts", [])} - initial_alerts))
            if reached or time.monotonic() >= deadline:
                return {"run_id": run_id, "reason": "terminal" if terminal else "condition" if reached else "timeout",
                        "timed_out": not reached, "progress": latest, "revision": latest.get("revision"),
                        "suggested_poll_seconds": latest.get("suggested_poll_seconds", 5)}
            time.sleep(min(1, max(0, deadline - time.monotonic())))
