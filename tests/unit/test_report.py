"""The report is a projection of the database, and it must not flatter it.

Two failure modes are worse than a missing page: showing an unobserved state as
healthy, and showing an unclassified failure as blank (which reads as "fine").
These tests pin those, plus the fact that the file is self-contained and escapes
whatever a log happened to contain.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import make_home

from rcf.core.config import Config
from rcf.core.errors import SpecError
from rcf.core.ids import now_utc
from rcf.core.models import (
    FailureKind,
    JobHandle,
    PlannedCommand,
    RunEvent,
    RunRecord,
    RunState,
)
from rcf.report import render_report
from rcf.service import RcfService, parse_since


@pytest.fixture
def service(tmp_path):
    home = make_home(tmp_path, nodes='[nodes.test]\nkind = "local"\n')
    svc = RcfService(Config.load(home), actor="test")
    yield svc
    svc.close()


def add_run(
    service: RcfService,
    run_id: str,
    state: RunState,
    *,
    project: str = "demo",
    exit_code: int | None = None,
    kind: FailureKind | None = None,
    age: timedelta = timedelta(0),
    duration: timedelta | None = None,
    diagnosis: str | None = None,
) -> None:
    created = now_utc() - age - (duration or timedelta(0))
    service.store.insert_run(
        RunRecord(
            run_id=run_id,
            node_id="test",
            executor="local",
            project_id=project,
            state=state,
            created_at=created,
            submitted_at=created,
            finished_at=created + duration if duration else None,
            exit_code=exit_code,
            state_observed_at=now_utc() - age,
            last_observed_at=now_utc() - age,
            command=PlannedCommand(argv=["true"], workdir="/tmp"),
            handle=JobHandle(kind="local", node_id="test", workdir="/tmp/run"),
            failure_kind=kind,
        )
    )
    if diagnosis:
        service.store.add_event(
            RunEvent(
                run_id=run_id,
                at=now_utc(),
                kind="classified",
                message=diagnosis,
                data={"retrospective": True},
            )
        )


def test_a_stale_state_is_never_presented_as_current(service):
    add_run(service, "run_live", RunState.RUNNING, age=timedelta(minutes=42))

    page = render_report(service.build_report())

    # The exact second depends on when the page was built, so the assertion is
    # about the label and the age being present, not about a rounded digit.
    assert "unverified 42m" in page
    assert "ago" in page
    assert "running</span>" not in page


def test_an_unclassified_failure_is_labelled_unknown_not_left_blank(service):
    add_run(service, "run_old", RunState.FAILED, exit_code=1)

    data = service.build_report()
    groups = {group["kind"]: group["count"] for group in data["failures"]}

    assert groups == {"unknown": 1}
    assert "unknown" in render_report(data)


def test_failures_are_grouped_by_kind_in_a_stable_order(service):
    add_run(service, "run_a", RunState.FAILED, kind=FailureKind.ENVIRONMENT, exit_code=127)
    add_run(service, "run_b", RunState.FAILED, kind=FailureKind.DOMAIN, exit_code=2)
    add_run(service, "run_c", RunState.FAILED, kind=FailureKind.DOMAIN, exit_code=2)

    data = service.build_report()

    assert [group["kind"] for group in data["failures"]] == ["domain", "environment"]
    assert data["failures"][0]["count"] == 2
    assert data["failures"][0]["share"] == pytest.approx(2 / 3)


def test_a_cancelled_run_does_not_count_against_the_success_rate(service):
    add_run(service, "run_ok", RunState.SUCCEEDED, duration=timedelta(seconds=10))
    add_run(service, "run_bad", RunState.FAILED, exit_code=1, duration=timedelta(seconds=20))
    add_run(service, "run_stop", RunState.CANCELLED, duration=timedelta(seconds=30))

    project = service.build_report()["projects"][0]

    assert project["total"] == 3
    assert project["succeeded"] == 1
    assert project["failed"] == 1
    assert project["success_rate"] == pytest.approx(0.5)
    assert project["p50_duration_s"] == 10
    assert project["max_duration_s"] == 10


def test_project_filter_applies_before_the_limit(service):
    add_run(service, "run_demo", RunState.SUCCEEDED, project="demo")
    for index in range(3):
        add_run(service, f"run_other_{index}", RunState.SUCCEEDED, project="other")

    data = service.build_report(project="demo", limit=1)

    assert [payload["run_id"] for payload in data["recent"]] == ["run_demo"]


def test_the_page_is_self_contained_and_escapes_what_the_log_said(service):
    add_run(
        service,
        "run_hostile",
        RunState.FAILED,
        exit_code=2,
        kind=FailureKind.DOMAIN,
        diagnosis='domain: <script>alert("x")</script> & done',
    )

    page = render_report(service.build_report())

    assert "<script" not in page.lower()
    assert "&lt;script&gt;" in page
    assert "http://" not in page and "https://" not in page
    assert "alert(&quot;x&quot;)" in page


def test_parse_since_reads_units_and_refuses_guesses():
    assert parse_since(None) is None
    assert parse_since("90m") == 5400
    assert parse_since("24h") == 86400
    assert parse_since("7d") == 604800
    assert parse_since("2w") == 1209600

    with pytest.raises(SpecError) as caught:
        parse_since("last week")
    assert caught.value.code == "spec_invalid"
    assert "90m" in caught.value.hint
