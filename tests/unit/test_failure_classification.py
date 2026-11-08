"""Failure classification: the same exit code, three different next actions.

``state=failed`` used to mean both "the node is missing numpy" and "the solver
finished and rejected this configuration".  On this profile's own history that was
18 rejected vs 6 broken out of 40 failures, so the classification is what makes a
failed-run list readable at all.

The log tails here are taken from real runs on the workstation node (job ids and
home paths shortened); they are the reason each rule exists.
"""

from __future__ import annotations

import pytest
from conftest import make_home

import rcf.runs.manager as manager_module
from rcf.core.config import Config
from rcf.core.ids import now_utc
from rcf.core.models import (
    FailureKind,
    JobHandle,
    JobStatus,
    PlannedCommand,
    RunRecord,
    RunState,
)
from rcf.runs.failure import classify_failure
from rcf.service import RcfService

VERDICT_JSON = """\
{"schema": "lc3d.q_physical_saved_probe/v1", "accepted": false, "status": "failed",
 "bulkRevision": "nodal_bulk_v2", "centreReleasedFixedScaleResidual": 0.000481}
"""

TRACEBACK = """\
Traceback (most recent call last):
  File "scripts/audit.py", line 66, in tet_slice
    if len(triangles) >= MAX_TRIANGLES: raise ValueError('slice exceeds limit')
ValueError: slice exceeds 20000 triangles; choose another plane
"""

ARGPARSE = """\
usage: hello.py [-h] [--seed SEED]
hello.py: error: argument --seed: invalid int value: 'abc'
"""


def classify(tail: str, *, exit_code: int | None = 2, state=RunState.FAILED):
    return classify_failure(state=state, exit_code=exit_code, tail=tail)


def test_a_structured_negative_verdict_is_domain():
    verdict = classify(VERDICT_JSON)

    assert verdict.kind is FailureKind.DOMAIN
    assert "accepted=False" in verdict.detail
    assert verdict.evidence["verdict"] == {"accepted": False}


def test_pretty_printed_json_is_found_by_walking_back_to_its_brace():
    tail = 'progress 10%\n{\n  "status": "failed",\n  "maxResidual": 0.4\n}\n'

    verdict = classify(tail)

    assert verdict.kind is FailureKind.DOMAIN
    assert "status=failed" in verdict.detail


def test_json_without_a_verdict_key_is_not_a_verdict_on_a_non_two_exit():
    # A run may print ordinary data; without exit 2 that is not a statement about
    # the result, so nothing is concluded.
    tail = '{"rows": 12, "columns": 4}\n'

    verdict = classify(tail, exit_code=1)

    assert verdict.kind is FailureKind.UNKNOWN


def test_a_report_followed_by_exit_two_is_read_as_a_verdict():
    # The pipelines on this profile print their report and exit 2 without naming a
    # status field; the detail says what the inference rests on.
    tail = '{"rows": 12, "columns": 4}\n'

    verdict = classify(tail, exit_code=2)

    assert verdict.kind is FailureKind.DOMAIN
    assert "no verdict field" in verdict.detail
    assert "rows" in verdict.detail


def test_a_missing_package_is_an_environment_failure():
    verdict = classify("Traceback...\nModuleNotFoundError: No module named 'numpy'\n", exit_code=1)

    assert verdict.kind is FailureKind.ENVIRONMENT
    assert verdict.detail.startswith("No module named")


def test_rejected_arguments_are_a_parameter_failure():
    verdict = classify(ARGPARSE)

    assert verdict.kind is FailureKind.PARAMETER
    assert "invalid int value" in verdict.detail


def test_a_raising_program_is_a_code_failure():
    verdict = classify(TRACEBACK, exit_code=1)

    assert verdict.kind is FailureKind.CODE
    assert "ValueError: slice exceeds" in verdict.detail


def test_exit_127_without_a_log_still_names_the_environment():
    assert classify("", exit_code=127).kind is FailureKind.ENVIRONMENT


def test_an_unknown_failure_stays_unknown_and_quotes_the_last_line():
    verdict = classify("starting\nsomething odd happened\n", exit_code=1)

    assert verdict.kind is FailureKind.UNKNOWN
    assert "no known failure pattern" in verdict.detail
    assert "something odd happened" in verdict.detail
    assert verdict.evidence["unmatched"] is True


def test_a_lost_run_is_unknown_rather_than_a_guess():
    verdict = classify("", exit_code=None, state=RunState.LOST)

    assert verdict.kind is FailureKind.UNKNOWN
    assert "lost" in verdict.detail


# -- persisted through a real refresh --------------------------------------- #


class _Finished:
    def __init__(self, log: str, exit_code: int = 2) -> None:
        self.log = log
        self.exit_code = exit_code

    def status(self, handle):  # noqa: ARG002 - executor contract
        return JobStatus(
            state=RunState.FAILED,
            exit_code=self.exit_code,
            detail="exit_code file written by the batch script",
            observed_at=now_utc(),
            finished_at=now_utc(),
        )

    def logs(self, handle, tail=40):  # noqa: ARG002 - executor contract
        return self.log


@pytest.fixture
def service(tmp_path):
    home = make_home(tmp_path, nodes='[nodes.test]\nkind = "local"\n')
    svc = RcfService(Config.load(home), actor="test")
    yield svc
    svc.close()


def save_failed(service: RcfService, run_id: str) -> None:
    service.store.insert_run(
        RunRecord(
            run_id=run_id,
            node_id="test",
            executor="local",
            state=RunState.RUNNING,
            created_at=now_utc(),
            submitted_at=now_utc(),
            state_observed_at=now_utc(),
            command=PlannedCommand(argv=["true"], workdir="/tmp"),
            handle=JobHandle(kind="local", node_id="test", workdir="/tmp/run"),
        )
    )


def test_the_kind_is_saved_with_the_failure_it_describes(service, monkeypatch):
    save_failed(service, "run_rejected")
    save_failed(service, "run_broken")
    monkeypatch.setattr(
        manager_module,
        "build_executor",
        lambda *a, **k: _Finished(VERDICT_JSON),
    )

    service.runs.refresh("run_rejected")

    # The column is written in the same observation as the terminal state, so a
    # list filter never needs the log again.
    assert service.store.get_run("run_rejected").failure_kind is FailureKind.DOMAIN
    rows = service.list_runs(failure_kind="domain")
    assert [row["run_id"] for row in rows] == ["run_rejected"]
    assert rows[0]["failure_kind"] == "domain"

    # A run that was never observed keeps no kind: nothing was concluded about it.
    assert service.store.get_run("run_broken").failure_kind is None
    assert service.list_runs(failure_kind="parameter") == []


def test_a_failed_run_carries_its_kind_into_the_event_trail(service, monkeypatch):
    save_failed(service, "run_rejected")
    monkeypatch.setattr(
        manager_module,
        "build_executor",
        lambda *a, **k: _Finished(VERDICT_JSON),
    )

    service.runs.refresh("run_rejected")

    events = [e for e in service.store.list_events("run_rejected") if e.kind == "diagnosis"]
    assert events and events[-1].data["kind"] == "domain"
    assert service.get_run("run_rejected", refresh=False)["failure_kind"] == "domain"


def test_a_glibc_mismatch_belongs_to_the_node_not_the_program():
    tail = (
        "Traceback (most recent call last):\n"
        "OSError: /lib64/libm.so.6: version `GLIBC_2.38' not found\n"
    )

    assert classify(tail, exit_code=1).kind is FailureKind.ENVIRONMENT


def test_a_range_violation_is_a_parameter_failure():
    tail = "ValueError: wallSeconds must be <=240 per model\n"

    assert classify(tail, exit_code=1).kind is FailureKind.PARAMETER


def test_a_failing_test_suite_is_reported_as_the_projects_own_failure():
    tail = "....\n1 failed, 69 passed in 24.96s\n"

    verdict = classify(tail, exit_code=1)

    assert verdict.kind is FailureKind.CODE
    assert "own tests" in verdict.detail


def test_a_missing_input_file_is_not_a_code_failure():
    # The upstream stage never produced it; patching the program would not help.
    tail = (
        "Traceback (most recent call last):\n"
        "FileNotFoundError: Stage 2A solver_data.npz not found\n"
    )

    verdict = classify(tail, exit_code=1)

    assert verdict.kind is FailureKind.INPUT
    assert "file the program needs is missing" in verdict.detail


def test_a_missing_field_in_the_input_data_is_an_input_failure():
    tail = "Traceback (most recent call last):\nKeyError: 'anchor_pretilt_deg'\n"

    verdict = classify(tail, exit_code=1)

    assert verdict.kind is FailureKind.INPUT
    assert "anchor_pretilt_deg" in verdict.detail


def test_a_key_error_word_in_a_report_does_not_count_as_a_raised_exception():
    # ``KeyError:`` is the marker; prose that mentions the name is not evidence.
    tail = '{"notes": "dodged a KeyError, nothing raised"}\n'

    assert classify(tail, exit_code=1).kind is FailureKind.UNKNOWN
