from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from rcf.progress.estimator import estimate

BASE = datetime(2026, 11, 1, tzinfo=UTC)
KEYS = {
    "status", "phase", "completed", "total", "fraction", "elapsed_seconds",
    "rate_per_second", "eta_seconds", "eta_range_seconds", "eta_scope", "confidence",
    "last_report_age_seconds", "last_advance_age_seconds", "alerts", "reason", "suggested_poll_seconds",
}


def sample(t, completed, *, seq=None, total=100, session="s1", phase="compute", **kwargs):
    return {
        "run_id": "run1", "session_id": session, "seq": int(t) + 1 if seq is None else seq,
        "timestamp": (BASE + timedelta(seconds=t)).isoformat(), "elapsed_seconds": float(t),
        "phase": phase, "completed": completed, "total": total, "metric": None,
        "eta_reason": None, "finished": False, **kwargs,
    }


def series():
    return [sample(0, 0), sample(10, 10), sample(20, 20)]


def codes(result):
    return {item["code"] for item in result["alerts"]}


def at(seconds):
    return BASE + timedelta(seconds=seconds)


def test_contract_and_linear_eta_uses_worker_elapsed():
    data = series()
    for item in data:
        item["elapsed_seconds"] *= 2
    result = estimate(data, now=at(20), started_at=BASE)
    assert set(result) == KEYS
    assert result["status"] == "progressing"
    assert result["rate_per_second"] == 0.5
    assert result["eta_seconds"] == 160
    assert result["eta_range_seconds"] == [160, 160]
    assert result["elapsed_seconds"] == 20
    assert result["fraction"] == 0.2
    assert result["eta_scope"] == "run"


def test_requires_two_advances_not_two_heartbeats():
    for data in (series()[:2], [sample(0, 0), sample(10, 0), sample(20, 0)]):
        result = estimate(data, now=at(20))
        assert result["status"] == "warming_up"
        assert result["eta_seconds"] is None


def test_heartbeat_does_not_hide_stall_or_inflate_speed():
    result = estimate([*series(), sample(350, 20)], now=at(350))
    assert result["status"] == "stalled"
    assert result["last_report_age_seconds"] == 0
    assert result["last_advance_age_seconds"] == 330
    assert result["eta_seconds"] is None
    assert "stalled_progress" in codes(result)
    resumed = estimate([*series(), sample(110, 20), sample(120, 30)], now=at(120))
    assert resumed["rate_per_second"] == 1  # median of [1, 1, .1], not [1,1,1]
    assert resumed["eta_range_seconds"][1] > resumed["eta_seconds"]


def test_stale_takes_precedence_and_suppresses_eta():
    result = estimate(series(), now=at(200))
    assert result["status"] == "stale"
    assert result["eta_seconds"] is None
    assert "stale_progress" in codes(result)


def test_adaptive_total_recomputes_eta_not_historical_fraction():
    data = series()
    data[-1]["total"] = 200
    result = estimate(data, now=at(20))
    assert result["eta_seconds"] == 180
    assert result["fraction"] == 0.1
    assert result["confidence"] == "low"
    assert "total_changed" in codes(result)


@pytest.mark.parametrize("change", ["session", "phase", "reset"])
def test_restart_phase_and_reset_do_not_mix_rates(change):
    data = series()
    additions = [sample(30, 0), sample(40, 1)]
    if change == "session":
        for index, item in enumerate(additions):
            item.update(session_id="s2", seq=index + 1, elapsed_seconds=float(index * 10))
    elif change == "phase":
        for item in additions:
            item["phase"] = "analysis"
    result = estimate(data + additions, now=at(40))
    assert result["status"] == "warming_up"
    assert result["eta_seconds"] is None
    assert result["rate_per_second"] is None


def test_multiple_phases_have_only_phase_eta_without_weights():
    data = [sample(0, 0, phase="prepare"), sample(5, 100, phase="prepare", finished=True),
            sample(10, 0), sample(20, 10), sample(30, 20)]
    result = estimate(data, now=at(30))
    assert result["eta_scope"] == "phase"
    assert result["eta_seconds"] == 80
    weighted = estimate(data, now=at(30), phase_weights={"prepare": 1, "compute": 3, "analysis": 2})
    assert weighted["eta_scope"] == "run"
    assert weighted["eta_seconds"] == pytest.approx(80 + 200 / 3)
    assert weighted["fraction"] == pytest.approx(1.6 / 6)
    missing = estimate(data, now=at(30), phase_weights={"compute": 3})
    assert missing["eta_scope"] == "phase"
    assert "incomplete_phase_model" in codes(missing)


@pytest.mark.parametrize("mode", ["convergence", "residual", "custom"])
def test_convergence_never_extrapolates_residual(mode):
    data = series()
    for index, item in enumerate(data):
        item["metric"] = 1 / (index + 1)
    result = estimate(data, now=at(20), mode=mode)
    assert result["status"] == "unavailable"
    assert result["eta_seconds"] is None
    assert result["eta_scope"] == "unavailable"


@pytest.mark.parametrize("mutate", [
    lambda item: item.update(seq=1),
    lambda item: item.update(seq=True),
    lambda item: item.update(elapsed_seconds=-1),
    lambda item: item.update(elapsed_seconds=0),
    lambda item: item.update(completed=float("nan")),
    lambda item: item.update(total=float("inf")),
    lambda item: item.update(metric=float("nan")),
    lambda item: item.update(run_id="another-run"),
    lambda item: item.update(finished="yes"),
    lambda item: item.update(timestamp="2026-11-01T00:00:20"),
    lambda item: item.pop("session_id"),
])
def test_corrupt_input_is_explicit_and_cannot_generate_eta(mutate):
    data = series()
    mutate(data[-1])
    result = estimate(data, now=at(20))
    assert result["status"] == "unavailable"
    assert result["eta_seconds"] is None
    assert "invalid_sample" in codes(result)


def test_duplicate_and_retired_session_are_rejected_even_if_more_valid_samples_follow():
    data = series()
    data.insert(2, data[1].copy())
    assert estimate(data, now=at(20))["eta_seconds"] is None
    data = [*series(), sample(30, 0, session="s2"), sample(40, 30)]
    assert "invalid_sample" in codes(estimate(data, now=at(40)))


@pytest.mark.parametrize("kind", ["future", "backwards"])
def test_clock_skew_is_explicit_and_no_fabricated_age(kind):
    data = series()
    data[-1]["timestamp"] = at(30 if kind == "future" else 5).isoformat()
    result = estimate(data, now=at(20))
    assert "clock_skew" in codes(result)
    assert result["last_report_age_seconds"] is None
    assert result["last_advance_age_seconds"] is None
    assert result["eta_seconds"] is None


def test_start_grace_empty_missing_started_at_and_budget():
    result = estimate([], now=at(20), started_at=BASE)
    assert result["status"] == "warming_up"
    assert result["elapsed_seconds"] == 20
    assert estimate([], now=at(61), started_at=BASE)["status"] == "unavailable"
    assert estimate([], now=at(20))["status"] == "unavailable"
    result = estimate([], now=at(20), started_at=at(30), budget_seconds=50)
    assert {"clock_skew", "budget_elapsed_unavailable"} <= codes(result)
    assert result["elapsed_seconds"] is None


@pytest.mark.parametrize("state", ["pending", "queued", "planned", "submitting"])
def test_pending_does_not_treat_queue_time_as_execution_eta(state):
    result = estimate(series(), now=at(5000), started_at=BASE, state=state, budget_seconds=10)
    assert result["status"] == "pending"
    assert result["elapsed_seconds"] is None
    assert result["eta_seconds"] is None
    assert not any(code.startswith("budget_") for code in codes(result))


def test_finished_does_not_imply_run_terminal():
    data = series()
    data[-1]["finished"] = True
    result = estimate(data, now=at(20))
    assert result["status"] != "terminal"
    assert result["eta_seconds"] is None
    for state in ("succeeded", "failed", "cancelled", "lost"):
        result = estimate(data, now=at(20), state=state)
        assert result["status"] == "terminal"
        assert result["eta_seconds"] is None
        assert result["suggested_poll_seconds"] == 0


def test_terminal_and_pending_without_samples_and_unknown_not_running():
    assert estimate([], state="failed")["status"] == "terminal"
    assert estimate([], state="pending")["status"] == "pending"
    result = estimate(series(), now=at(20), state="unknown")
    assert result["status"] == "unavailable"
    assert result["eta_seconds"] is None


def test_budget_elapsed_and_predicted_overrun_are_distinct():
    result = estimate(series(), now=at(20), started_at=BASE, budget_seconds=90)
    assert "budget_at_risk" in codes(result)
    result = estimate(series(), now=at(20), started_at=BASE, budget_seconds=10)
    assert "budget_exceeded" in codes(result)


def test_slowdown_and_robust_range():
    data = [sample(0, 0), sample(10, 10), sample(20, 20), sample(30, 30),
            sample(130, 40), sample(230, 50)]
    result = estimate(data, now=at(230), baseline_rate=2.1)
    assert "slowdown" in codes(result)
    assert result["rate_per_second"] == 1
    assert result["eta_range_seconds"][0] <= result["eta_seconds"] <= result["eta_range_seconds"][1]
    assert "slowdown" in codes(estimate(data, now=at(230)))


@pytest.mark.parametrize("total", [None, 0, 10])
def test_missing_zero_or_exceeded_total_has_no_eta(total):
    data = series()
    for item in data:
        item["total"] = total
    result = estimate(data, now=at(20))
    assert result["eta_seconds"] is None
    assert result["status"] == "unavailable"


def test_missing_real_start_does_not_use_reporter_creation_for_budget_clock():
    result = estimate(series(), now=at(20), budget_seconds=100)
    assert result["elapsed_seconds"] is None
    assert "budget_elapsed_unavailable" in codes(result)


def test_extreme_finite_values_do_not_produce_infinite_eta():
    data = [sample(0, 0, total=1e308), sample(10, 1e-307, total=1e308),
            sample(20, 2e-307, total=1e308)]
    result = estimate(data, now=at(20))
    assert result["eta_seconds"] is None
    assert result["status"] == "unavailable"
    data[-1]["completed"] = 10 ** 1000
    assert "invalid_sample" in codes(estimate(data, now=at(20)))


def test_reporter_can_explain_no_eta_and_confidence_improves_with_stable_history():
    data = [sample(t, t) for t in range(0, 60, 10)]
    assert estimate(data, now=at(50))["confidence"] == "high"
    data[-1]["eta_reason"] = "adaptive workload not yet bounded"
    result = estimate(data, now=at(50))
    assert result["eta_seconds"] is None
    assert result["reason"] == data[-1]["eta_reason"]


@pytest.mark.parametrize("kwargs", [
    {"heartbeat_timeout_seconds": -1}, {"stall_timeout_seconds": math.nan},
    {"budget_seconds": math.inf}, {"baseline_rate": 0}, {"phase_weights": {"compute": -1}},
])
def test_invalid_configuration_fails_explicitly(kwargs):
    with pytest.raises(ValueError):
        estimate([], **kwargs)
