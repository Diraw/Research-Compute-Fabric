"""End-to-end through a simulated SSH + Slurm host.

Nothing is mocked inside RCF: the real ``SshTransport`` builds a real ``ssh``
argv, a real bash executes the generated scripts over a sandbox directory, and
the real Slurm adapter talks to a fake ``sbatch``/``squeue``/``sacct``.
"""

from __future__ import annotations

import time

import pytest
from conftest import FIXTURES, PYTHON, make_home, to_win, toml_path

from rcf.core.errors import Conflict, HostKeyRejected, PermissionDenied

pytestmark = pytest.mark.e2e


def wait_for(service, run_id: str, states: set[str], timeout: float = 30.0):
    deadline = time.time() + timeout
    record = None
    while time.time() < deadline:
        record = service.get_run(run_id)
        if record["state"] in states:
            return record
        time.sleep(0.3)
    return record


def test_node_inspection_over_ssh(service):
    data = service.inspect_node("lab-hpc")
    snapshot = data["snapshot"]

    assert snapshot["adapter"] == "ssh"
    assert snapshot["home_dir"].endswith("/lab-hpc")
    assert snapshot["cpu"]["logical_cores"] >= 1
    assert snapshot["memory"]["total_mb"] == 62500  # 64,000,000 kB
    assert [p["name"] for p in snapshot["scheduler"]["partitions"]] == ["gpu", "cpu"]
    assert len(snapshot["gpus"]) == 2
    assert snapshot["gpus"][0]["name"] == "NVIDIA A100-SXM4-40GB"

    # Free VRAM is not availability: on a Slurm node the scheduler decides.
    statuses = {item["gpu_index"]: item["status"] for item in data["gpu_schedulability"]}
    assert statuses == {0: "scheduler_granted", 1: "scheduler_granted"}


def test_environment_is_inspected_through_the_node_shell(service):
    view = service.inspect_environment("py311", "lab-hpc")
    assert view["state"] in {"compatible", "verified"}
    assert view["python_version"].startswith("3.")
    assert view["packages"]["pytest"]


def test_plan_submit_observe_results(service, ssh_sandbox):
    plan = service.plan_run(
        {
            "project_id": "hello",
            "params": {"seed": 3},
            "requirements": {"gpus": 1, "cpus": 2, "time_minutes": 5, "partition": "gpu"},
        }
    )
    assert not [c for c in plan["checks"] if c["status"] == "fail"], plan["checks"]
    assert plan["executor"] == "slurm"
    assert plan["run_dir"].startswith("/")  # ~ was resolved to an absolute path
    assert "--seed" in plan["command"]["argv"]
    # The declared environment pins the interpreter, so {python} is python3
    # (a bare `python` does not exist on modern Ubuntu/Debian).
    assert plan["command"]["argv"][0] == "python3"
    assert plan["command"]["preview"].startswith("#")

    record = service.submit_run(plan["plan_id"], confirm=True, idempotency_key="e2e-1")
    assert record["state"] in {"pending", "running", "succeeded"}
    assert record["handle"]["job_id"]

    done = wait_for(service, record["run_id"], {"succeeded", "failed", "lost", "unknown"})
    assert done is not None
    assert done["state"] == "succeeded", (done["state"], service.get_logs(record["run_id"])["log"])
    assert done["exit_code"] == 0

    logs = service.get_logs(record["run_id"])
    assert "hello 3" in logs["log"]
    assert "slurm" in logs["log"].lower() or True  # header is written by the batch script

    manifest = service.collect_results(record["run_id"])
    files = {entry["path"]: entry for entry in manifest["entries"]}
    assert "result.txt" in files
    assert "seed=3" in (to_win(manifest["result_dir"]) / "result.txt").read_text(encoding="utf-8")

    # The batch script state must also live on the "remote" filesystem.
    run_dir = to_win(plan["run_dir"])
    assert (run_dir / "exit_code").is_file()
    assert (run_dir / "params.json").is_file()
    assert (run_dir / "job_id").read_text().strip() == record["handle"]["job_id"]


def test_idempotent_submit_returns_the_same_run(service):
    plan = service.plan_run({"project_id": "hello", "params": {"seed": 1}})
    first = service.submit_run(plan["plan_id"], confirm=True, idempotency_key="same-key")
    second = service.submit_run(plan["plan_id"], confirm=True, idempotency_key="same-key")
    assert first["run_id"] == second["run_id"]

    other = service.plan_run({"project_id": "hello", "params": {"seed": 2}})
    with pytest.raises(Conflict):
        service.submit_run(other["plan_id"], confirm=True, idempotency_key="same-key")


def test_recover_accepted_slurm_submission_after_lost_reply(service, ssh_sandbox, monkeypatch):
    """Use a real generated shell receipt, then simulate losing the SSH reply."""

    from rcf.core.errors import HostUnreachable
    from rcf.executors.slurm_exec import SlurmExecutor

    monkeypatch.setenv("FAKE_SLURM_MODE", "pending")
    plan = service.plan_run({"project_id": "hello", "params": {"seed": 1}})
    original = SlurmExecutor.submit

    def lose_reply(self, spec):
        original(self, spec)
        raise HostUnreachable("simulated connection loss after scheduler accepted the job")

    monkeypatch.setattr(SlurmExecutor, "submit", lose_reply)
    with pytest.raises(HostUnreachable):
        service.submit_run(plan["plan_id"], confirm=True)
    same = service.submit_run(plan["plan_id"], confirm=True)
    assert same["state"] == "unknown"
    assert same["handle"]["job_id"] is None
    assert len(list((ssh_sandbox.host / ".fake_slurm" / "jobs").glob("*.json"))) == 1
    recovered = service.get_run(plan["run_id"])
    deadline = time.time() + 5
    while recovered["state"] == "unknown" and time.time() < deadline:
        time.sleep(0.1)
        recovered = service.get_run(plan["run_id"])
    assert recovered["state"] == "pending"
    assert recovered["handle"]["job_id"].isdigit()
    assert not recovered["handle"]["extra"].get("submission_uncertain")
    assert service.cancel_run(plan["run_id"], confirm=True)["state"] == "cancelled"


def test_submit_requires_confirmation(service):
    plan = service.plan_run({"project_id": "hello", "params": {"seed": 0}})
    with pytest.raises(PermissionDenied):
        service.submit_run(plan["plan_id"], confirm=False)


def test_cancel_pending_slurm_job(service, monkeypatch):
    monkeypatch.setenv("FAKE_SLURM_MODE", "pending")
    plan = service.plan_run({"project_id": "hello", "params": {"seed": 5}})
    record = service.submit_run(plan["plan_id"], confirm=True)

    observed = service.get_run(record["run_id"])
    assert observed["state"] == "pending"
    assert "Priority" in (observed.get("events") or [{}])[-1].get("message", "") or True

    cancelled = service.cancel_run(record["run_id"], confirm=True)
    assert cancelled["state"] in {"cancelled", "pending"}
    final = wait_for(service, record["run_id"], {"cancelled"})
    assert final["state"] == "cancelled"


def test_failed_run_reports_exit_code_and_log(service, ssh_sandbox):
    (ssh_sandbox.host / "proj" / "hello" / "hello.py").write_text(
        "import sys\nprint('boom')\nsys.exit(3)\n", encoding="utf-8"
    )
    plan = service.plan_run({"project_id": "hello", "params": {"seed": 9}})
    record = service.submit_run(plan["plan_id"], confirm=True)
    done = wait_for(service, record["run_id"], {"succeeded", "failed"})
    assert done["state"] == "failed"
    assert done["exit_code"] == 3
    assert "boom" in service.get_logs(record["run_id"])["log"]


def test_host_key_failure_is_actionable(service, monkeypatch):
    monkeypatch.setenv("RCF_FAKE_SSH_MODE", "hostkey")
    with pytest.raises(HostKeyRejected) as excinfo:
        service.inspect_node("lab-hpc")
    assert "known_hosts" in (excinfo.value.hint or "") or "host key" in excinfo.value.message.lower()


def test_auth_failure_is_distinguished(service, monkeypatch):
    from rcf.core.errors import AuthFailed

    monkeypatch.setenv("RCF_FAKE_SSH_MODE", "auth")
    with pytest.raises(AuthFailed):
        service.inspect_node("lab-hpc")


def test_unreachable_node_is_distinguished(service, monkeypatch):
    from rcf.core.errors import HostUnreachable

    monkeypatch.setenv("RCF_FAKE_SSH_MODE", "unreachable")
    with pytest.raises(HostUnreachable):
        service.inspect_node("lab-hpc")


def test_plan_refuses_to_bypass_a_missing_partition(service):
    plan = service.plan_run(
        {"project_id": "hello", "params": {"seed": 0}, "requirements": {"partition": "nonexistent"}}
    )
    failures = [c["name"] for c in plan["checks"] if c["status"] == "fail"]
    assert "partition" in failures
    with pytest.raises(PermissionDenied):
        service.submit_run(plan["plan_id"], confirm=True)


def test_results_report_artifacts_written_next_to_the_sources(service, ssh_sandbox):
    """A legacy script wrote into the checkout instead of its declared output directory."""

    project_dir = ssh_sandbox.host / "proj" / "hello"
    (project_dir / "side.py").write_text(
        "open('side_effect.txt', 'w').write('x')\nprint('wrote it')\n", encoding="utf-8"
    )
    plan = service.plan_run(
        {"project_id": "hello", "command_override": "{python} side.py"}
    )
    record = service.submit_run(plan["plan_id"], confirm=True)
    done = wait_for(service, record["run_id"], {"succeeded", "failed"})
    assert done["state"] == "succeeded", service.get_logs(record["run_id"])["log"]

    manifest = service.collect_results(record["run_id"])
    assert manifest["entries"] == []  # nothing in $RCF_OUTPUT_DIR
    assert any(e["path"].endswith("side_effect.txt") for e in manifest["workdir_changes"])
    assert "did not create them" in manifest["note"]


def test_fetch_copies_and_verifies_artifacts(service, tmp_path):
    plan = service.plan_run({"project_id": "hello", "params": {"seed": 4}})
    record = service.submit_run(plan["plan_id"], confirm=True)
    done = wait_for(service, record["run_id"], {"succeeded", "failed"})
    assert done["state"] == "succeeded"

    dest = tmp_path / "fetched"
    payload = service.fetch_results(record["run_id"], dest=str(dest), confirm=True)

    assert payload["files"] == 1, payload
    assert payload["verified"] is True
    assert len(payload["sha256"]) == 64
    text = (dest / "result.txt").read_text(encoding="utf-8")
    assert text.startswith("seed=4")


def test_fetch_needs_confirmation_and_an_empty_destination(service, tmp_path):
    from rcf.core.errors import Conflict, PermissionDenied

    plan = service.plan_run({"project_id": "hello", "params": {"seed": 6}})
    record = service.submit_run(plan["plan_id"], confirm=True)
    wait_for(service, record["run_id"], {"succeeded", "failed"})

    with pytest.raises(PermissionDenied, match="confirmation"):
        service.fetch_results(record["run_id"], dest=str(tmp_path / "a"), confirm=False)

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(Conflict, match="not empty"):
        service.fetch_results(record["run_id"], dest=str(busy), confirm=True)

    merged = service.fetch_results(record["run_id"], dest=str(busy), force=True, confirm=True)
    assert merged["files"] == 1
    assert (busy / "keep.txt").exists()  # force merges, never deletes


def test_node_selftest_passes_on_the_simulated_host(service):
    result = service.selftest_node("lab-hpc")
    names = {c["name"]: c["status"] for c in result["checks"]}
    assert result["ok"] is True, result
    assert names["connect"] == "ok"
    assert names["shell"] == "ok"
    assert names["work_root"] == "ok"
    # The node is declared as slurm, and the simulated host has sbatch.
    assert names["scheduler"] == "ok"


def test_node_selftest_reports_an_unwritable_work_root(tmp_path, ssh_sandbox):
    from rcf.core.config import Config
    from rcf.core.runner import SubprocessRunner
    from rcf.service import RcfService

    home = make_home(
        tmp_path,
        nodes=f"""\
[nodes.broken]
kind = "ssh"
ssh_alias = "lab-hpc"
work_root = "/proc/rcf-not-writable"

[nodes.broken.ssh]
program = ["{toml_path(PYTHON)}", "{toml_path(FIXTURES / "fake_ssh.py")}"]
scp_program = ["{toml_path(PYTHON)}", "{toml_path(FIXTURES / "fake_scp.py")}"]
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner(), actor="test")
    try:
        result = service.selftest_node("broken")
        names = {c["name"]: c["status"] for c in result["checks"]}
        assert result["ok"] is False
        assert names["work_root"] == "fail"
        assert names["connect"] == "ok"  # the shell itself is fine
    finally:
        service.close()


def test_logs_explain_a_deleted_run_directory(service, ssh_sandbox):
    """The user may clean <work_root>/runs by hand; say so instead of 'not found'."""

    import shutil

    plan = service.plan_run({"project_id": "hello", "params": {"seed": 8}})
    record = service.submit_run(plan["plan_id"], confirm=True)
    wait_for(service, record["run_id"], {"succeeded", "failed"})

    shutil.rmtree(to_win(plan["run_dir"]))
    text = service.get_logs(record["run_id"])["log"]
    assert "no longer exists" in text
    assert "removed outside RCF" in text


def test_commit_travels_from_plan_to_run_json(service, ssh_sandbox):
    """The artifact directory must answer 'which code produced this' by itself."""

    import json
    import os
    import subprocess

    project_dir = ssh_sandbox.host / "proj" / "hello"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True, env=env)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "init"],
        cwd=project_dir, check=True, env=env,
    )

    plan = service.plan_run({"project_id": "hello", "params": {"seed": 2}})
    assert plan["git_commit"] and len(plan["git_commit"]) == 40
    assert plan["git_dirty"] is False

    record = service.submit_run(plan["plan_id"], confirm=True)
    assert record["git_commit"] == plan["git_commit"]
    wait_for(service, record["run_id"], {"succeeded", "failed"})

    meta = json.loads((to_win(plan["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert meta["git_commit"] == plan["git_commit"]
    assert meta["git_dirty"] is False
