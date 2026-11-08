"""Two records a reader can hold: one run's manifest, and the difference between two.

Both are pure functions over payloads, so these tests use real shapes from the
database rather than a cluster.  The interesting cases are the dishonest ones: an
unreadable output listing must not become "no outputs", and two files of equal size
must not become "same".
"""

from __future__ import annotations

from rcf.core.ids import now_utc
from rcf.runs.diff import compare_runs
from rcf.runs.manifest import build_manifest


def payload(**overrides):
    base = {
        "run_id": "run_a",
        "plan_id": "plan_a",
        "project_id": "lc3d",
        "node_id": "hpc",
        "executor": "slurm",
        "state": "succeeded",
        "exit_code": 0,
        "failure_kind": None,
        "sweep_id": None,
        "created_at": "2026-11-08T00:00:00Z",
        "submitted_at": "2026-11-08T00:00:10Z",
        "finished_at": "2026-11-08T00:01:40Z",
        "state_observed_at": "2026-11-08T00:01:40Z",
        "params": {"seed": 1},
        "command": {
            "argv": ["/usr/bin/python3", "run.py", "--seed", "1"],
            "workdir": "/proj",
            "preview": "# environment\nexport RCF_RUN_ID=run_a\n# command\n/usr/bin/python3 run.py --seed 1",
            "shell": False,
            "env": {"RCF_RUN_ID": "run_a"},
        },
        "git_commit": "a" * 40,
        "git_dirty": False,
        "env_id": "lc3d-cpu-hpc",
        "env_fingerprint": "abc123",
        "inputs": {"digest": "f" * 64, "total_bytes": 10, "files": []},
        "result_dir": "/runs/run_a/outputs",
        "handle": {"kind": "slurm", "node_id": "hpc", "job_id": "1234"},
    }
    base.update(overrides)
    return base


# -- manifest --------------------------------------------------------------- #


def test_the_manifest_carries_what_a_reader_needs_to_reproduce_the_run():
    manifest = build_manifest(
        payload(), outputs=None, outputs_error=None, generated_at="2026-11-08T01:00:00Z"
    )

    assert manifest["schema"] == "rcf.run.manifest/v1"
    assert manifest["run"]["run_id"] == "run_a"
    assert manifest["run"]["duration_seconds"] == 90
    assert manifest["command"]["argv"][0] == "/usr/bin/python3"
    assert manifest["params"] == {"seed": 1}
    assert manifest["code"]["git_commit"] == "a" * 40
    assert manifest["environment"]["fingerprint"] == "abc123"
    assert manifest["inputs"]["digest"] == "f" * 64


def test_outputs_that_were_not_listed_are_not_reported_as_empty():
    manifest = build_manifest(
        payload(), outputs=None, outputs_error=None, generated_at=now_utc().isoformat()
    )

    assert manifest["outputs"]["listed"] is False
    assert manifest["outputs"]["files"] == []
    assert manifest["outputs"]["error"] is None


def test_an_unreadable_listing_is_recorded_as_an_error_not_as_no_files():
    manifest = build_manifest(
        payload(),
        outputs=None,
        outputs_error="host_unreachable: no route to host",
        generated_at=now_utc().isoformat(),
    )

    assert manifest["outputs"]["listed"] is False
    assert "no route" in manifest["outputs"]["error"]


def test_a_listed_output_keeps_its_hash():
    listed = {
        "entries": [{"path": "result.txt", "size": 32, "sha256": "deadbeef"}],
        "truncated": False,
    }

    manifest = build_manifest(
        payload(), outputs=listed, outputs_error=None, generated_at=now_utc().isoformat()
    )

    assert manifest["outputs"]["listed"] is True
    assert manifest["outputs"]["files"] == [
        {"path": "result.txt", "size": 32, "sha256": "deadbeef"}
    ]


# -- diff ------------------------------------------------------------------- #


def test_params_are_reported_per_key_with_the_direction_of_the_change():
    left = payload(params={"seed": 1, "mode": "fast", "old": 1})
    right = payload(params={"seed": 2, "mode": "fast", "new": 1})

    result = compare_runs(left, right)
    rows = {row["key"]: row for row in result["params"]}

    assert rows["seed"]["status"] == "changed"
    assert rows["mode"]["status"] == "same"
    assert rows["old"]["status"] == "only left"
    assert rows["new"]["status"] == "only right"
    assert result["changed"] is True


def test_identical_runs_report_no_differences():
    result = compare_runs(payload(), payload())

    assert result["changed"] is False
    assert all(item["same"] for item in result["provenance"])
    assert all(item["same"] for item in result["outcome"])


def test_the_command_is_compared_without_the_per_run_environment():
    # Same argv, different run ids in the environment: this is not a difference in
    # what ran, and reporting it as one would make every pair look different.
    left = payload()
    right = payload(run_id="run_b")
    right["command"]["env"] = {"RCF_RUN_ID": "run_b"}

    assert compare_runs(left, right)["command"]["same"] is True


def test_an_artifact_of_the_same_size_but_different_content_is_changed():
    left_outputs = {"entries": [{"path": "q.csv", "size": 32, "sha256": "aaa"}], "truncated": False}
    right_outputs = {"entries": [{"path": "q.csv", "size": 32, "sha256": "bbb"}], "truncated": False}

    result = compare_runs(
        payload(), payload(), outputs_left=left_outputs, outputs_right=right_outputs
    )

    assert [row["path"] for row in result["outputs"]["changed"]] == ["q.csv"]
    assert result["outputs"]["same"] == 0


def test_a_missing_hash_is_not_evidence_of_identical_content():
    left_outputs = {"entries": [{"path": "q.csv", "size": 32}], "truncated": False}
    right_outputs = {"entries": [{"path": "q.csv", "size": 32}], "truncated": False}

    result = compare_runs(
        payload(), payload(), outputs_left=left_outputs, outputs_right=right_outputs
    )

    assert len(result["outputs"]["changed"]) == 1


def test_outputs_that_could_not_be_read_say_so_instead_of_matching():
    result = compare_runs(
        payload(), payload(), outputs_left=None, outputs_right=None, outputs_reason="host unreachable"
    )

    assert result["outputs"]["available"] is False
    assert result["outputs"]["reason"] == "host unreachable"


def test_a_run_that_was_never_submitted_says_it_has_no_outputs_to_list(tmp_path):
    from conftest import make_home

    from rcf.core.config import Config
    from rcf.core.models import PlannedCommand, RunRecord, RunState
    from rcf.service import RcfService

    home = make_home(tmp_path, nodes='[nodes.test]\nkind = "local"\n')
    service = RcfService(Config.load(home), actor="test")
    try:
        service.store.insert_run(
            RunRecord(
                run_id="run_planned",
                node_id="test",
                executor="local",
                state=RunState.PLANNED,
                created_at=now_utc(),
                command=PlannedCommand(argv=["true"], workdir="/tmp"),
            )
        )

        manifest = service.run_manifest("run_planned")
    finally:
        service.close()

    assert manifest["run"]["run_id"] == "run_planned"
    assert manifest["outputs"]["listed"] is False
    assert manifest["outputs"]["error"] is None
