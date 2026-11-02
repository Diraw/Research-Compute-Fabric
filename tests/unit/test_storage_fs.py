from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

import pytest

from rcf.core.errors import ExecutorError, SpecError
from rcf.core.models import NodeSpec
from rcf.core.runner import CommandResult, ScriptedRunner, SubprocessRunner
from rcf.storage.fs import StorageFS, validate_relative
from rcf.storage.fs_script import SCRIPT

NODE = NodeSpec(id="local", kind="local")


@pytest.mark.parametrize("path", ["", ".", "..", "a/../b", "/tmp/x", "C:/x", "C:x", "a\\b", "a//b", "a/./b", "a/", "a\x00b"])
def test_invalid_relative(path: str) -> None:
    with pytest.raises(SpecError):
        validate_relative(path)


@pytest.mark.parametrize("path", ["a b", "a\tb", "a\nb", "sub/a'\";$(x)"])
def test_valid_relative(path: str) -> None:
    assert validate_relative(path) == path


def test_script_python36_syntax() -> None:
    ast.parse(SCRIPT, feature_version=(3, 6))


def test_remote_protocol_preserves_literal_control_characters() -> None:
    import base64
    import json

    node = NodeSpec(id="remote", kind="ssh", ssh_alias="fake-only")
    runner = ScriptedRunner(responses=[CommandResult(("ssh",), 0, stdout='{"errors": [], "complete": true}')])
    name = "sub/a\tline\n';$()"
    StorageFS(runner).inspect(node, "/fixture root", [name])
    assert len(runner.calls) == 1
    assert runner.calls[0][-1] == "bash -s"
    script = runner.inputs[0]
    assert script is not None
    encoded = script.split("REQUEST = ", 1)[1].splitlines()[0]
    request = json.loads(base64.b64decode(ast.literal_eval(encoded)))
    assert request["paths"] == [name]
    assert request["root"] == "/fixture root"
    assert name not in script


@pytest.mark.parametrize("budget", [-1, float("inf"), float("nan")])
def test_invalid_time_bounds(budget: float) -> None:
    with pytest.raises(SpecError):
        StorageFS(ScriptedRunner()).scan(NODE, "/fixture", max_seconds=budget)


def test_scan_inspect_delete(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "sub").mkdir()
    path = "sub/a b';$.txt"
    (tmp_path / path).write_bytes(b"abc")
    inventory = fs.scan(NODE, str(tmp_path))
    assert inventory["complete"] is True
    assert inventory["logical_bytes"] == 3
    assert inventory["file_count"] == 1
    assert inventory["root"] == str(tmp_path.resolve())
    assert {e["path"] for e in inventory["entries"]} == {"sub", path}
    assert all(e["sha256"] is None for e in inventory["entries"])
    inspected = fs.inspect(NODE, str(tmp_path), [path])
    assert inspected["complete"] is True
    entry = inspected["entries"][0]
    assert entry["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert isinstance(entry["device"], str)
    assert isinstance(entry["inode"], str)
    result = fs.delete(NODE, str(tmp_path), [entry])
    assert result == {"deleted": [path], "missing": [], "blocked": [], "errors": [], "logical_deleted_bytes": 3}
    assert (tmp_path / "sub").is_dir()
    assert fs.delete(NODE, str(tmp_path), [entry])["missing"] == [path]


def test_partial_and_missing_are_not_empty(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    for name in ("a", "b", "c"):
        (tmp_path / name).write_bytes(b"data")
    limited = fs.scan(NODE, str(tmp_path), max_entries=1)
    assert limited["complete"] is False
    assert len(limited["entries"]) <= 1
    assert limited["errors"]
    timed = fs.scan(NODE, str(tmp_path), max_seconds=0)
    assert timed["complete"] is False
    assert timed["errors"]
    absent = fs.scan(NODE, str(tmp_path / "missing"))
    assert absent["complete"] is False
    assert absent["errors"]
    missing = fs.inspect(NODE, str(tmp_path), ["missing"])
    assert missing["complete"] is False
    assert missing["missing"] == ["missing"]


def test_changed_and_unhashed_files_blocked(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "a").write_bytes(b"abc")
    (tmp_path / "b").write_bytes(b"xyz")
    entries = fs.inspect(NODE, str(tmp_path), ["a", "b"])["entries"]
    before = (tmp_path / "a").stat()
    (tmp_path / "a").write_bytes(b"ABC")
    os.utime(tmp_path / "a", ns=(before.st_atime_ns, before.st_mtime_ns))
    entries[1]["sha256"] = None
    result = fs.delete(NODE, str(tmp_path), entries)
    assert not result["deleted"]
    assert {b["path"] for b in result["blocked"]} == {"a", "b"}
    assert (tmp_path / "a").read_bytes() == b"ABC"


def test_link_and_directory_are_never_deleted(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "safe").write_text("keep")
    entries = fs.inspect(NODE, str(tmp_path), ["dir"])["entries"]
    assert fs.delete(NODE, str(tmp_path), entries)["blocked"]
    try:
        (tmp_path / "link").symlink_to(tmp_path / "dir", target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    scan = fs.scan(NODE, str(tmp_path))
    assert "link/safe" not in {e["path"] for e in scan["entries"]}
    assert next(e for e in scan["entries"] if e["path"] == "link")["kind"] == "symlink"
    assert fs.inspect(NODE, str(tmp_path), ["link/safe"])["complete"] is False
    assert fs.scan(NODE, str(tmp_path / "link"))["complete"] is False


def test_batch_partial_deletion(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    for name in ("a", "b"):
        (tmp_path / name).write_bytes(b"abc")
    entries = fs.inspect(NODE, str(tmp_path), ["a", "b"])["entries"]
    (tmp_path / "b").write_bytes(b"edited")
    result = fs.delete(NODE, str(tmp_path), entries)
    assert result["deleted"] == ["a"]
    assert result["logical_deleted_bytes"] == 3
    assert result["blocked"][0]["path"] == "b"


def test_home_and_fs_roots_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "a").write_bytes(b"abc")
    entry = fs.inspect(NODE, str(tmp_path), ["a"])["entries"][0]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = fs.delete(NODE, str(tmp_path), [entry])
    assert result["blocked"]
    assert (tmp_path / "a").exists()
    assert fs.delete(NODE, str(tmp_path.anchor), [entry])["blocked"]


def test_inspect_does_not_recurse(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "child").write_bytes(b"abc")
    result = fs.inspect(NODE, str(tmp_path), ["sub"], hashes=False)
    assert [e["path"] for e in result["entries"]] == ["sub"]
    assert result["entries"][0]["sha256"] is None


def test_hash_budget_exhaustion_is_incomplete(tmp_path: Path) -> None:
    import json
    import sys

    (tmp_path / "a").write_bytes(b"abc")
    request = {"op": "inspect", "root": str(tmp_path), "paths": ["a"], "hashes": True, "max_seconds": 0}
    result = SubprocessRunner().run([sys.executable, "-c", SCRIPT], stdin=json.dumps(request))
    data = json.loads(result.stdout)
    assert result.ok
    assert data["complete"] is False
    assert not data["entries"]
    assert data["errors"]


def test_missing_parent_and_missing_file_are_explicit(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a").write_bytes(b"abc")
    entry = fs.inspect(NODE, str(tmp_path), ["sub/a"])["entries"][0]
    (tmp_path / "sub" / "a").unlink()
    (tmp_path / "sub").rmdir()
    assert fs.delete(NODE, str(tmp_path), [entry])["missing"] == ["sub/a"]


def test_hardlink_logical_and_allocated(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "a").write_bytes(b"abc")
    os.link(tmp_path / "a", tmp_path / "b")
    result = fs.scan(NODE, str(tmp_path))
    assert result["complete"] is True
    assert result["logical_bytes"] == 6
    assert result["file_count"] == 2
    assert {e["nlink"] for e in result["entries"]} == {2}
    if result["allocated_bytes"] is not None:
        assert result["allocated_bytes"] == result["entries"][0]["allocated_bytes"]


def test_replaced_root_evidence_blocks(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"abc")
    entry = fs.inspect(NODE, str(root), ["a"])["entries"][0]
    root.rename(tmp_path / "previous")
    root.mkdir()
    (root / "a").write_bytes(b"abc")
    assert fs.delete(NODE, str(root), [entry])["blocked"]
    assert (root / "a").read_bytes() == b"abc"


@pytest.mark.skipif(os.name != "nt", reason="Win32 path alias regression")
def test_windows_alternate_stream_and_alias_rejected(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "a").write_bytes(b"abc")
    with pytest.raises(SpecError):
        fs.inspect(NODE, str(tmp_path), ["a:stream"])
    for name in ["file:stream", "a.", "a ", "NUL", "CON.txt"]:
        inspected = fs.inspect(NODE, str(tmp_path), [name])
        assert inspected["complete"] is False
        assert inspected["errors"]
    assert (tmp_path / "a").read_bytes() == b"abc"


def test_windows_junction_blocked(tmp_path: Path) -> None:
    import sys

    if os.name != "nt":
        pytest.skip("Windows junction regression")
    runner = SubprocessRunner()
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    outside.mkdir()
    root.mkdir()
    (outside / "keep").write_bytes(b"safe")
    junction = root / "junction"
    created = runner.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)])
    assert created.ok, created.stderr
    try:
        fs = StorageFS(runner)
        result = fs.scan(NODE, str(root))
        assert result["complete"] is True
        assert [(e["path"], e["kind"]) for e in result["entries"]] == [("junction", "symlink")]
        assert fs.inspect(NODE, str(root), ["junction/keep"])["complete"] is False
        assert fs.scan(NODE, str(junction))["complete"] is False
        assert (outside / "keep").read_bytes() == b"safe"
        assert sys.platform == "win32"
    finally:
        junction.rmdir()


def test_runner_failure_is_structured() -> None:
    runner = ScriptedRunner(responses=[CommandResult(("python",), 1, stderr="no interpreter")])
    with pytest.raises(ExecutorError):
        StorageFS(runner).scan(NODE, "/fixture")


def test_no_shell_globbing_or_expansion(tmp_path: Path) -> None:
    fs = StorageFS(SubprocessRunner())
    (tmp_path / "keep").write_bytes(b"abc")
    result = fs.delete(NODE, str(tmp_path), [{"path": "../keep"}, {"path": "*"}])
    assert len(result["blocked"]) == 2
    assert (tmp_path / "keep").exists()
