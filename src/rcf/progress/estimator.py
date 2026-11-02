"""Conservative, pure ETA estimation from untrusted JSONL records.

Worker monotonic time supplies rate; controller UTC time supplies ages and the
run budget clock. No sample, including ``finished``, can change run state.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

_FIELDS = {
    "run_id", "session_id", "seq", "timestamp", "elapsed_seconds", "phase",
    "completed", "total", "metric", "eta_reason", "finished",
}
_TERMINAL = {"succeeded", "failed", "cancelled", "canceled", "lost", "completed"}
_PENDING = {"pending", "queued", "planned", "submitting"}


def _finite(value: Any, nonnegative: bool = False) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and (not nonnegative or value >= 0))
    except OverflowError:
        return False


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must have a timezone")
    return value.astimezone(UTC)


def _sample(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not _FIELDS.issubset(value):
        raise ValueError("missing required progress fields")
    for key in ("run_id", "session_id", "phase"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"invalid {key}")
    if isinstance(value["seq"], bool) or not isinstance(value["seq"], int) or value["seq"] < 1:
        raise ValueError("invalid seq")
    for key in ("elapsed_seconds", "completed"):
        if not _finite(value[key], True):
            raise ValueError(f"invalid {key}")
    if value["total"] is not None and not _finite(value["total"], True):
        raise ValueError("invalid total")
    if value["metric"] is not None and not _finite(value["metric"]):
        raise ValueError("invalid metric")
    if value["eta_reason"] is not None and not isinstance(value["eta_reason"], str):
        raise ValueError("invalid eta_reason")
    if not isinstance(value["finished"], bool):
        raise ValueError("invalid finished")
    return {**value, "_time": _utc(value["timestamp"])}


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def estimate(
    samples: list[dict], *, now: datetime | None = None, started_at: datetime | None = None,
    state: str = "running", heartbeat_timeout_seconds: float = 120,
    stall_timeout_seconds: float = 300, start_grace_seconds: float = 60,
    budget_seconds: float | None = None, baseline_rate: float | None = None,
    mode: str = "steps", phase_weights: dict[str, float] | None = None,
    age_offset_seconds: float = 0.0,
) -> dict:
    """Return explicit unknowns instead of extrapolating corrupt or insufficient data.

    Two positive advances (three checkpoints) are required. Speeds use the last
    eight advances of the current contiguous phase/session/reset segment. Bounds
    are robust empirical ranges, not probabilistic confidence intervals. Explicit
    phase weights describe relative cost; they permit cross-phase extrapolation
    only when every observed phase is weighted and all past phases finished.
    """
    now = _utc(now or datetime.now(UTC))
    if not _finite(age_offset_seconds) or age_offset_seconds < 0:
        raise ValueError("age_offset_seconds must be finite and nonnegative")
    age_now = now - timedelta(seconds=age_offset_seconds)
    for name, value in (("heartbeat_timeout_seconds", heartbeat_timeout_seconds),
                        ("stall_timeout_seconds", stall_timeout_seconds),
                        ("start_grace_seconds", start_grace_seconds)):
        if not _finite(value, True):
            raise ValueError(f"{name} must be finite and nonnegative")
    for name, value in (("budget_seconds", budget_seconds), ("baseline_rate", baseline_rate)):
        if value is not None and (not _finite(value) or value <= 0):
            raise ValueError(f"{name} must be finite and positive")
    if phase_weights is not None and (
        not isinstance(phase_weights, dict) or not phase_weights
        or any(not isinstance(k, str) or not k or not _finite(v) or v <= 0
               for k, v in phase_weights.items())
    ):
        raise ValueError("phase_weights must map phase names to finite positive costs")

    if phase_weights is not None and not _finite(sum(phase_weights.values())):
        raise ValueError("phase weight sum must be finite")

    result = {
        "status": "unavailable", "phase": None, "completed": None, "total": None,
        "fraction": None, "elapsed_seconds": None, "rate_per_second": None,
        "eta_seconds": None, "eta_range_seconds": None, "eta_scope": "unavailable",
        "confidence": "none", "last_report_age_seconds": None,
        "last_advance_age_seconds": None, "alerts": [], "reason": None,
        "suggested_poll_seconds": 15.0,
    }

    def alert(code: str, message: str) -> None:
        if not any(item["code"] == code for item in result["alerts"]):
            result["alerts"].append({"code": code, "message": message})

    elapsed = None
    if started_at is not None:
        try:
            elapsed = (now - _utc(started_at)).total_seconds()
            if elapsed < 0:
                elapsed = None
                alert("clock_skew", "Run start is in the controller's future.")
        except (ValueError, TypeError, OverflowError):
            alert("invalid_started_at", "Run start is not a timezone-aware timestamp.")
    result["elapsed_seconds"] = elapsed

    records = []
    previous_by_session = {}
    retired_sessions = set()
    current_session = None
    run_id = None
    corrupt = False
    for index, raw in enumerate(samples):
        try:
            item = _sample(raw)
            session = item["session_id"]
            if run_id is not None and item["run_id"] != run_id:
                raise ValueError("mixed run IDs")
            if session in retired_sessions:
                raise ValueError("retired session reappeared")
            previous = previous_by_session.get(session)
            if previous is not None and (item["seq"] <= previous["seq"]
                                        or item["elapsed_seconds"] < previous["elapsed_seconds"]):
                raise ValueError("duplicate/out-of-order seq or decreasing elapsed time")
            if current_session is not None and session != current_session:
                retired_sessions.add(current_session)
                alert("session_changed", "Reporter restarted; rates use only the new session.")
            current_session, run_id = session, item["run_id"]
            previous_by_session[session] = item
            records.append(item)
        except (ValueError, TypeError, OverflowError) as exc:
            corrupt = True
            alert("invalid_sample", f"Progress record {index} rejected: {exc}.")

    normalized_state = str(state).lower()
    pending = normalized_state in _PENDING
    terminal = normalized_state in _TERMINAL
    if records:
        last = records[-1]
        result.update(phase=last["phase"], completed=last["completed"], total=last["total"])
        ages = [(age_now - item["_time"]).total_seconds() for item in records]
        clock_bad = any(age < 0 for age in ages) or any(
            right["_time"] < left["_time"] for left, right in zip(records, records[1:], strict=False)
        )
        if clock_bad:
            alert("clock_skew", "Reporter wall clock is future-dated or moved backwards; ages are untrusted.")
        else:
            result["last_report_age_seconds"] = ages[-1]
        # Reporter construction can lag actual execution start. Session-local
        # elapsed is safe for rates, not for the whole-run budget clock.
        if last["total"] is not None and last["total"] > 0:
            result["fraction"] = min(1.0, last["completed"] / last["total"])
        # Isolate resets; keep a heartbeat out of the advance checkpoint list.
        segment = []
        for item in records:
            if segment:
                prev = segment[-1]
                if (item["session_id"] != prev["session_id"] or item["phase"] != prev["phase"]
                        or item["completed"] < prev["completed"]):
                    if item["completed"] < prev["completed"] and item["phase"] == prev["phase"]:
                        alert("progress_reset", "Progress counter reset; old speed history was discarded.")
                    segment = []
            segment.append(item)
        advances = [segment[0]]
        last_advance = segment[0]
        for item in segment[1:]:
            if item["completed"] > last_advance["completed"]:
                advances.append(item)
                last_advance = item
        if not clock_bad:
            result["last_advance_age_seconds"] = (age_now - last_advance["_time"]).total_seconds()
        totals = {item["total"] for item in segment}
        if len(totals) > 1:
            alert("total_changed", "Adaptive total changed; fraction and remaining work were recalculated.")
        rates = []
        for left, right in zip(advances[-9:], advances[-9:][1:], strict=False):
            duration = right["elapsed_seconds"] - left["elapsed_seconds"]
            if duration > 0:
                rate = (right["completed"] - left["completed"]) / duration
                if math.isfinite(rate) and rate > 0:
                    rates.append(rate)
        rate = _quantile(rates, 0.5) if len(rates) >= 2 else None
        if rate is not None:
            result["rate_per_second"] = rate
            reference = baseline_rate
            if reference is None and len(rates) >= 4:
                reference = _quantile(rates[:-2], 0.5)
                recent = _quantile(rates[-2:], 0.5)
            else:
                recent = rate
            if reference is not None and recent < reference * 0.5:
                alert("slowdown", "Recent progress rate is below half the reference rate.")

        if terminal:
            result.update(status="terminal", reason=f"Run state is {normalized_state}.", suggested_poll_seconds=0.0)
        elif pending:
            result.update(status="pending", elapsed_seconds=None, rate_per_second=None,
                          reason="Run is queued/not started; execution ETA excludes queue time.",
                          suggested_poll_seconds=30.0)
        elif corrupt:
            result["reason"] = "Malformed or out-of-order progress records prevent a trustworthy ETA."
        elif clock_bad:
            result["reason"] = "Reporter clock skew prevents trustworthy freshness and stall assessment."
        elif result["last_report_age_seconds"] > heartbeat_timeout_seconds:
            result.update(status="stale", reason="No fresh reporter heartbeat.", suggested_poll_seconds=30.0)
            alert("stale_progress", "Progress reporter heartbeat timed out.")
        elif result["last_advance_age_seconds"] > stall_timeout_seconds:
            result.update(status="stalled", reason="Heartbeats are arriving but completed work has not advanced.")
            alert("stalled_progress", "Progress has not advanced within the stall timeout.")
        elif mode not in {"steps", "phases"}:
            result["reason"] = "Convergence/custom metrics have no safe linear ETA model."
        elif last["eta_reason"]:
            result["reason"] = last["eta_reason"]
        elif last["finished"]:
            result["reason"] = "Phase finished; awaiting run state or next phase, not evidence of run success."
        elif last["total"] is None or last["total"] <= 0:
            result["reason"] = "No positive total work is known."
        elif last["completed"] > last["total"]:
            result["reason"] = "Completed work exceeds total; awaiting a corrected adaptive total."
            alert("total_exceeded", "Completed work exceeds the declared total.")
        elif rate is None:
            result.update(status="warming_up", reason="At least two positive timed advances are required.",
                          suggested_poll_seconds=5.0)
        else:
            work = last["total"] - last["completed"]
            scope = "phase" if mode == "phases" or len({item["phase"] for item in records}) > 1 else "run"
            if phase_weights is not None:
                observed = {item["phase"] for item in records}
                completed_phases = {item["phase"] for item in records
                                    if item["finished"] and item["phase"] != last["phase"]}
                prior = observed - {last["phase"]}
                if observed.issubset(phase_weights) and prior.issubset(completed_phases):
                    total_weight = sum(phase_weights.values())
                    done_weight = sum(phase_weights[name] for name in completed_phases)
                    current_weight = phase_weights[last["phase"]]
                    future_weight = total_weight - done_weight - current_weight
                    work += max(0.0, future_weight) / current_weight * last["total"]
                    result["fraction"] = (done_weight / total_weight
                                          + current_weight / total_weight * result["fraction"])
                    scope = "run"
                else:
                    scope = "phase"
                    alert("incomplete_phase_model", "Phase weights do not cover all phases or prior phases are unfinished.")
            low, high = _quantile(rates, 0.2), _quantile(rates, 0.8)
            eta, lo, hi = work / rate, work / high, work / low
            if all(math.isfinite(number) for number in (eta, lo, hi)):
                confidence = "high" if len(rates) >= 5 and high / low < 1.5 else "medium"
                if len(totals) > 1 or scope == "phase" or phase_weights is not None:
                    confidence = "low"
                result.update(status="progressing", eta_seconds=eta, eta_range_seconds=[lo, hi],
                              eta_scope=scope, confidence=confidence, reason="Rolling median of local monotonic progress rates.",
                              suggested_poll_seconds=max(5.0, min(30.0, eta / 10)))
            else:
                result["reason"] = "ETA arithmetic exceeded the finite numeric range."
    elif terminal:
        result.update(status="terminal", reason=f"Run state is {normalized_state}.", suggested_poll_seconds=0.0)
    elif pending:
        result.update(status="pending", elapsed_seconds=None,
                      reason="Run is queued/not started; no execution ETA.", suggested_poll_seconds=30.0)
    elif corrupt:
        result["reason"] = "No valid progress records."
    elif elapsed is not None and elapsed <= start_grace_seconds:
        result.update(status="warming_up", reason="Within startup grace; waiting for the first report.",
                      suggested_poll_seconds=5.0)
    else:
        result["reason"] = "No progress samples; ETA unavailable."

    if normalized_state not in _TERMINAL | _PENDING | {"running"}:
        result.update(status="unavailable", eta_seconds=None, eta_range_seconds=None,
                      eta_scope="unavailable", confidence="none", reason="Run state is not known to be running.")
    if budget_seconds is not None and not pending and not terminal:
        if elapsed is None:
            alert("budget_elapsed_unavailable", "Cannot assess elapsed execution budget without a trustworthy start time.")
        elif elapsed >= budget_seconds:
            alert("budget_exceeded", "Elapsed execution time has reached the configured budget.")
        elif result["eta_seconds"] is not None and result["eta_scope"] == "run":
            if elapsed + result["eta_seconds"] > budget_seconds:
                alert("budget_at_risk", "Estimated completion exceeds the execution time budget.")
    return result
