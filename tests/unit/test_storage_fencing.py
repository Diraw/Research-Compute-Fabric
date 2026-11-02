from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rcf.core.errors import ExecutorError, SpecError
from rcf.core.models import NodeSpec
from rcf.core.runner import ScriptedRunner, SubprocessRunner
from rcf.storage.fs import StorageFS
from rcf.storage.fs_script import SCRIPT

NODE = NodeSpec(id="local", kind="local")


def journal(root: Path, plan: str = "plan") -> Path:
    return root / ".rcf-storage" / "operations" / (hashlib.sha256(plan.encode()).hexdigest() + ".jsonl")


def test_fencing_and_complete_report_idempotency(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    file = tmp_path / "a"
    file.write_bytes(b"first")
    entries = fs.inspect(NODE, str(tmp_path), ["a"])["entries"]
    first = fs.prepare_delete(NODE, str(tmp_path), "plan")
    assert first["generation"] == 1
    assert first["previous_report"] is None
    second = fs.prepare_delete(NODE, str(tmp_path), "plan")
    assert second["generation"] == 2
    with pytest.raises(ExecutorError, match="fenced"):
        fs.delete(NODE, str(tmp_path), entries, operation=first)
    assert file.read_bytes() == b"first"
    report = fs.delete(NODE, str(tmp_path), entries, operation=second)
    assert report["deleted"] == ["a"]
    file.write_bytes(b"new file must survive")
    new_entries = fs.inspect(NODE, str(tmp_path), ["a"])["entries"]
    assert fs.delete(NODE, str(tmp_path), new_entries, operation=second) == report
    assert file.read_bytes() == b"new file must survive"
    third = fs.prepare_delete(NODE, str(tmp_path), "plan")
    assert third["previous_report"] == report
    assert third["generation"] == 3
    assert {e["path"] for e in fs.scan(NODE, str(tmp_path))["entries"]} == {"a"}


def test_busy_no_generation_change_and_crash_releases_real_os_lock(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "a").write_bytes(b"keep")
    entries = fs.inspect(NODE, str(tmp_path), ["a"])["entries"]
    operation = fs.prepare_delete(NODE, str(tmp_path), "plan")
    # Execute the actual helper, with the first deletion paused AFTER the
    # running record has been fsynced. Kill the process, not a mocked locker.
    harness = SCRIPT.replace("if __name__ == '__main__':", "if False:") + "\n" + (
        "original_delete = delete_one\n"
        "def delete_one(scope, entry):\n"
        "    print('LOCKED', flush=True)\n"
        "    time.sleep(120)\n"
        "    return original_delete(scope, entry)\n"
        "run(" + repr({"op": "delete", "root": str(tmp_path), "entries": entries,
                       "operation": operation, "max_seconds": 300}) + ")\n"
    )
    child = subprocess.Popen([sys.executable, "-u", "-c", "import sys; exec(sys.stdin.read())"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write(harness)
        child.stdin.close()
        assert child.stdout.readline().strip() == "LOCKED"
        before = journal(tmp_path).read_bytes()
        with pytest.raises(ExecutorError, match="busy"):
            fs.prepare_delete(NODE, str(tmp_path), "plan")
        assert journal(tmp_path).read_bytes() == before
        with pytest.raises(ExecutorError, match="busy"):
            fs.delete(NODE, str(tmp_path), entries, operation=operation)
    finally:
        child.kill()
        child.wait(timeout=10)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()
    assert (tmp_path / "a").read_bytes() == b"keep"
    assert json.loads(journal(tmp_path).read_text().splitlines()[-1])["state"] == "running"
    with pytest.raises(ExecutorError, match="already started"):
        fs.delete(NODE, str(tmp_path), entries, operation=operation)
    recovered = fs.prepare_delete(NODE, str(tmp_path), "plan")
    assert recovered["generation"] == 2
    assert "unknown" in recovered["previous_report"]["errors"][0]["reason"]
    with pytest.raises(ExecutorError, match="fenced"):
        fs.delete(NODE, str(tmp_path), entries, operation=operation)
    assert fs.delete(NODE, str(tmp_path), entries, operation=recovered)["deleted"] == ["a"]


def test_crash_after_unlink_never_replays_spent_generation(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "a").write_bytes(b"old")
    entries = fs.inspect(NODE, str(tmp_path), ["a"])["entries"]
    operation = fs.prepare_delete(NODE, str(tmp_path), "plan")
    harness = SCRIPT.replace("if __name__ == '__main__':", "if False:") + "\n" + (
        "original_delete = delete_one\n"
        "def delete_one(scope, entry):\n"
        "    original_delete(scope, entry)\n"
        "    os._exit(77)\n"
        "run(" + repr({"op": "delete", "root": str(tmp_path), "entries": entries,
                       "operation": operation, "max_seconds": 30}) + ")\n"
    )
    child = subprocess.run([sys.executable, "-c", "import sys; exec(sys.stdin.read())"],
                           input=harness, capture_output=True, text=True, timeout=30)
    assert child.returncode == 77
    assert not (tmp_path / "a").exists()
    (tmp_path / "a").write_bytes(b"new")
    replacement = fs.inspect(NODE, str(tmp_path), ["a"])["entries"]
    with pytest.raises(ExecutorError, match="already started"):
        fs.delete(NODE, str(tmp_path), replacement, operation=operation)
    resumed = fs.prepare_delete(NODE, str(tmp_path), "plan")
    assert resumed["generation"] == 2
    assert resumed["previous_report"]["errors"]
    with pytest.raises(ExecutorError, match="fenced"):
        fs.delete(NODE, str(tmp_path), replacement, operation=operation)
    assert (tmp_path / "a").read_bytes() == b"new"


@pytest.mark.parametrize("component", [".rcf-storage", "operations", "lock", "journal"])
def test_metadata_symlink_blocked(tmp_path: Path, component: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    fs = StorageFS(SubprocessRunner())
    key = hashlib.sha256(b"plan").hexdigest()
    if component == ".rcf-storage":
        path, target = root / component, outside
    elif component == "operations":
        (root / ".rcf-storage").mkdir()
        path, target = root / ".rcf-storage" / component, outside
    else:
        fs.prepare_delete(NODE, str(root), "plan")
        path = root / ".rcf-storage" / "operations" / (key + (".lock" if component == "lock" else ".jsonl"))
        path.unlink()
        target = outside / "precious"
        target.write_bytes(b"never follow or truncate")
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ExecutorError):
        fs.prepare_delete(NODE, str(root), "plan")
    if target.is_file():
        assert target.read_bytes() == b"never follow or truncate"


def test_metadata_hardlink_and_torn_journal_fail_closed(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    fs.prepare_delete(NODE, str(tmp_path), "plan")
    manifest = journal(tmp_path)
    alias = tmp_path / "journal-alias"
    os.link(manifest, alias)
    with pytest.raises(ExecutorError, match="hardlink"):
        fs.prepare_delete(NODE, str(tmp_path), "plan")
    alias.unlink()
    with manifest.open("ab") as stream:
        stream.write(b'{"generation":999')
    before = manifest.read_bytes()
    with pytest.raises(ExecutorError, match="torn"):
        fs.prepare_delete(NODE, str(tmp_path), "plan")
    assert manifest.read_bytes() == before


def test_replaced_root_with_copied_metadata_blocked(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    root = tmp_path / "root"
    root.mkdir()
    operation = fs.prepare_delete(NODE, str(root), "plan")
    root.rename(tmp_path / "old")
    shutil.copytree(tmp_path / "old", root)
    with pytest.raises(ExecutorError, match="invalid operation journal"):
        fs.delete(NODE, str(root), [], operation=operation)
    with pytest.raises(ExecutorError, match="invalid operation journal"):
        fs.prepare_delete(NODE, str(root), "plan")


def test_protected_metadata_cannot_be_deleted_even_legacy(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    fs.prepare_delete(NODE, str(tmp_path), "plan")
    path = journal(tmp_path).relative_to(tmp_path).as_posix()
    assert fs.inspect(NODE, str(tmp_path), [path])["errors"]
    assert "protected" in fs.delete(NODE, str(tmp_path), [{"path": path}])["blocked"][0]["reason"]


@pytest.mark.parametrize("operation", [{}, {"operation_id": "p", "generation": True},
                                       {"operation_id": "p", "generation": 0}])
def test_invalid_operation_rejected_without_launch(operation: dict) -> None:
    with pytest.raises(SpecError):
        StorageFS(ScriptedRunner()).delete(NODE, "unused", [], operation=operation)


def test_helper_remains_python36_and_windows_argv_safe() -> None:
    ast.parse(SCRIPT, feature_version=(3, 6))
    assert len(subprocess.list2cmdline([sys.executable, "-c", SCRIPT])) < 32767
