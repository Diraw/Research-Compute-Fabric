"""Read-only prefix inspection: temporary paths and simulated SSH only."""
from __future__ import annotations

import io
import json
import os
import posixpath
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import FIXTURES, PYTHON, Sandbox

from rcf.core.errors import ExecutorError
from rcf.core.models import NodeSpec, SshOptions
from rcf.core.runner import ScriptedRunner, SubprocessRunner, fail, ok
from rcf.environments.ownership import check_prefix
from rcf.environments.path_guard import _INSPECT_SCRIPT, inspect_prefix_path


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    # macOS's default temp directory can itself contain a /var symlink. Test
    # fixtures use its real spelling; the production guard must not resolve it.
    return tmp_path.resolve()


def inspect(path: Path | str):
    return inspect_prefix_path(NodeSpec(id="local-test", kind="local"), str(path), SubprocessRunner())


def test_nonexistent_prefix_below_file_is_not_a_new_safe_environment(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    obstacle = root / "not-a-directory"
    obstacle.write_text("do not modify", encoding="utf-8")
    target = obstacle / "new-environment"
    # This demonstrates why lexical ownership is necessary but insufficient.
    assert check_prefix(path=str(target), home=None, managed_roots=[str(root)], exists=False,
                        known_to_rcf=False, declared_managed=False).allowed
    result = inspect(target)
    assert result.blocked
    assert "directory" in result.reason
    assert obstacle.read_text(encoding="utf-8") == "do not modify"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction requires cmd.exe")
def test_missing_prefix_below_junction_cannot_escape_managed_root(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    junction = root / "linked"
    created = SubprocessRunner().run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)], timeout=10,
    )
    assert created.ok, created.combined
    try:
        target = junction / "missing" / "new-environment"
        assert check_prefix(path=str(target), home=None, managed_roots=[str(root)], exists=False,
                            known_to_rcf=False, declared_managed=False).allowed
        result = inspect(target)
        assert result.blocked
        assert "reparse" in result.reason or "link" in result.reason
        assert not (external / "missing").exists()
    finally:
        os.rmdir(junction)


def test_existing_and_missing_prefixes_are_read_only_snapshots(tmp_path: Path) -> None:
    existing = tmp_path / "managed" / "existing"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("untouched", encoding="utf-8")
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    found = inspect(existing)
    assert found.allowed
    assert found.exists is True
    assert found.checked_paths[-1] == str(existing)
    assert found.missing_from is None
    missing = existing.parent / "new" / "nested" / "prefix"
    absent = inspect(missing)
    assert absent.allowed
    assert absent.exists is False
    assert absent.checked_paths[-1] == str(existing.parent)
    assert absent.missing_from == str(existing.parent / "new")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "untouched"


def test_target_itself_must_be_directory(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.touch()
    result = inspect(target)
    assert result.blocked
    assert "not a directory" in result.reason


@pytest.mark.parametrize("dangling", [False, True])
def test_ancestor_symlink_is_rejected_before_missing_suffix(tmp_path: Path, dangling: bool) -> None:
    external = tmp_path / "external"
    if not dangling:
        external.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink privilege unavailable: {exc}")
    assert inspect(link).blocked
    result = inspect(link / "managed" / "new" / "prefix")
    assert result.blocked
    assert "symlink" in result.reason or "reparse" in result.reason


def test_prefix_is_not_allowed_to_be_relative_or_parent_traversal(tmp_path: Path) -> None:
    for raw in ("relative/environment", "~another-user/env", str(tmp_path / ".." / "new"), "", "\x00"):
        assert inspect(raw).blocked, raw


def test_local_home_expansion_uses_target_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path))
    result = inspect("~/managed/new-prefix")
    assert result.allowed
    assert result.path == str(tmp_path / "managed" / "new-prefix")
    assert result.exists is False


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 namespace aliases are Windows-only")
@pytest.mark.parametrize("suffix", ["/NUL", "/AUX.txt", "/COM1", "/ambiguous.", "/ambiguous ", "/env:stream"])
def test_windows_ambiguous_path_names_are_blocked(tmp_path: Path, suffix: str) -> None:
    assert inspect(str(tmp_path) + suffix).blocked


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 device/UNC paths are Windows-only")
@pytest.mark.parametrize("path", [r"\\?\C:\envs\x", r"\\server\share\env", r"\envs\x", "C:envs/x"])
def test_windows_nonlocal_namespaces_fail_before_io(path: str) -> None:
    result = inspect(path)
    assert result.blocked


def _script_result(path: str, lstat, stat_probe=None):
    output = io.StringIO()
    original_stat = os.stat
    supports_dir_fd = os.supports_dir_fd | {stat_probe} if stat_probe else os.supports_dir_fd
    supports_nofollow = os.supports_follow_symlinks | {stat_probe} if stat_probe else os.supports_follow_symlinks
    with patch("sys.stdin", io.StringIO(json.dumps({"path": path}))), patch("sys.stdout", output), \
            patch("os.lstat", lstat), patch("os.stat", stat_probe or original_stat), \
            patch("os.supports_dir_fd", supports_dir_fd), patch("os.supports_follow_symlinks", supports_nofollow):
        exec(compile(_INSPECT_SCRIPT, "<prefix-inspection-test>", "exec"), {})
    return json.loads(output.getvalue())


def test_permission_error_is_not_treated_as_missing_prefix(tmp_path: Path) -> None:
    real_lstat = os.lstat
    real_stat = os.stat

    def denied(path):
        if str(path) == str(tmp_path):
            raise PermissionError("synthetic inaccessible ancestor")
        return real_lstat(path)

    def denied_stat(path, *args, **kwargs):
        if str(path) == tmp_path.name:
            raise PermissionError("synthetic inaccessible ancestor")
        return real_stat(path, *args, **kwargs)

    result = _script_result(str(tmp_path / "new"), denied, denied_stat)
    assert result["allowed"] is False
    assert result["exists"] is None
    assert "inaccessible" in result["reason"]


@pytest.mark.skipif(sys.platform != "win32", reason="st_file_attributes belongs to Win32 inspection")
@pytest.mark.parametrize("attributes", [None, stat.FILE_ATTRIBUTE_REPARSE_POINT])
def test_unavailable_or_non_symlink_reparse_attributes_fail_closed(tmp_path: Path, attributes) -> None:
    real_lstat = os.lstat

    def unknown(path):
        if str(path) == str(tmp_path):
            info = real_lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev,
                                   st_ino=info.st_ino, st_file_attributes=attributes)
        return real_lstat(path)

    result = _script_result(str(tmp_path / "new"), unknown)
    assert result["allowed"] is False
    assert "reparse" in result["reason"]


@pytest.mark.parametrize("response", [
    ok(""), ok("not-json"), ok("[]"), ok('{"allowed": true}'),
    ok('{"allowed": "false", "reason": "no"}'),
    ok('{"allowed": true, "reason": "ok", "path": "/x", "exists": false, "checked": []}'),
    fail("python3 unavailable", returncode=127), ExecutorError("synthetic transport error"),
])
def test_probe_errors_fail_closed_without_missing_existence_assumption(response) -> None:
    runner = ScriptedRunner(responses=[response])
    result = inspect_prefix_path(NodeSpec(id="remote-test", kind="ssh", ssh_alias="fake"), "/safe/env", runner)
    assert result.blocked
    assert result.exists is None


def remote_node() -> NodeSpec:
    return NodeSpec(id="lab-hpc", kind="ssh", ssh_alias="lab-hpc",
                    ssh=SshOptions(program=[PYTHON, str(FIXTURES / "fake_ssh.py")]))


def test_remote_request_is_stdin_data_not_shell_code() -> None:
    runner = ScriptedRunner(responses=[fail("synthetic")])
    malicious = "/managed/'\";$(touch PWNED)\nRCF_PREFIX_REQUEST\nenv"
    assert inspect_prefix_path(remote_node(), malicious, runner).blocked
    assert runner.calls[0][-1] == "bash -s"
    command, payload = runner.inputs[0].split(" <<'RCF_PREFIX_REQUEST'\n", 1)
    assert malicious not in command
    body, end = payload.rsplit("\n", 2)[:2]
    assert json.loads(body) == {"path": malicious}
    assert end == "RCF_PREFIX_REQUEST"


def test_fake_ssh_literal_path_is_read_only(ssh_sandbox: Sandbox) -> None:
    target = ssh_sandbox.host / "literal's ;$(touch PWNED)" / "new-env"
    result = inspect_prefix_path(remote_node(), str(target), SubprocessRunner())
    assert result.allowed, result.reason
    assert result.exists is False
    assert not target.parent.exists()
    assert not (ssh_sandbox.host / "PWNED").exists()


def test_fake_ssh_home_expansion_and_file_ancestor(ssh_sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake host runs native Windows Python through Bash; USERPROFILE is its
    # native equivalent of POSIX HOME. No real account/home directory is probed.
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(ssh_sandbox.host))
    runner = SubprocessRunner()
    safe = inspect_prefix_path(remote_node(), "~/managed/new-env", runner)
    assert safe.allowed, safe.reason
    assert safe.path == str(ssh_sandbox.host / "managed" / "new-env")
    file = ssh_sandbox.host / "managed"
    file.write_text("untouched", encoding="utf-8")
    blocked = inspect_prefix_path(remote_node(), "~/managed/new-env", runner)
    assert blocked.blocked
    assert "directory" in blocked.reason
    assert file.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("unsafe", [None, "symlink", "file", "permission", "changed"])
def test_posix_dir_fd_traversal_uses_only_existing_components(unsafe: str | None) -> None:
    """Exercise the POSIX protocol on Windows without pretending to run Linux."""
    nodes = {path: SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=i + 1)
             for i, path in enumerate(("/", "/home", "/home/researcher", "/home/researcher/managed"))}
    managed = "/home/researcher/managed"
    if unsafe in {"symlink", "file"}:
        nodes[managed].st_mode = stat.S_IFLNK if unsafe == "symlink" else stat.S_IFREG
    descriptors: dict[int, str] = {}
    closed: list[int] = []
    inspected: list[str] = []

    def probe(path, *, dir_fd=None, follow_symlinks=False):
        assert follow_symlinks is False
        path = posixpath.join(descriptors[dir_fd], path) if dir_fd is not None else path
        inspected.append(path)
        if unsafe == "permission" and path == managed:
            raise PermissionError("synthetic denied")
        if path not in nodes:
            raise FileNotFoundError(path)
        return nodes[path]

    def open_dir(path, flags, *, dir_fd=None):
        assert flags == 7  # O_RDONLY | O_DIRECTORY | O_NOFOLLOW, never a write/create.
        path = posixpath.join(descriptors[dir_fd], path) if dir_fd is not None else path
        fd = len(descriptors) + 100
        descriptors[fd] = path
        return fd

    def fstat(fd):
        info = nodes[descriptors[fd]]
        if unsafe == "changed" and descriptors[fd] == managed:
            return SimpleNamespace(st_dev=1, st_ino=9999)
        return info

    path_module = SimpleNamespace(
        expanduser=lambda text: text.replace("~", "/home/researcher", 1), isabs=posixpath.isabs,
        normpath=posixpath.normpath, splitdrive=posixpath.splitdrive, join=posixpath.join,
    )
    fake_os = SimpleNamespace(
        name="posix", path=path_module, sep="/", stat=probe, lstat=probe, open=open_dir,
        fstat=fstat, close=closed.append, supports_dir_fd={probe, open_dir},
        supports_follow_symlinks={probe}, O_RDONLY=1, O_DIRECTORY=2, O_NOFOLLOW=4,
    )
    output = io.StringIO()
    request = json.dumps({"path": "~/managed/missing/nested/env"})
    with patch.dict(sys.modules, {"os": fake_os}), patch("sys.stdin", io.StringIO(request)), \
            patch("sys.stdout", output):
        exec(compile(_INSPECT_SCRIPT, "<posix-prefix-test>", "exec"), {})
    result = json.loads(output.getvalue())
    assert result["allowed"] is (unsafe is None)
    assert closed == list(reversed(descriptors))
    assert "/home/researcher/managed/missing/nested" not in inspected
    if unsafe is None:
        assert result["exists"] is False
        assert result["missing_from"] == managed + "/missing"
        assert len(result["checked"]) == 4
    else:
        assert result["exists"] is None
        assert result["reason"]


def test_fake_ssh_auth_failure_blocks(ssh_sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCF_FAKE_SSH_MODE", "auth")
    result = inspect_prefix_path(remote_node(), str(ssh_sandbox.host / "env"), SubprocessRunner())
    assert result.blocked
    assert result.exists is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction requires cmd.exe")
def test_fake_ssh_junction_ancestor_blocks_missing_prefix(ssh_sandbox: Sandbox) -> None:
    external = ssh_sandbox.root / "external"
    external.mkdir()
    junction = ssh_sandbox.host / "linked"
    runner = SubprocessRunner()
    created = runner.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)], timeout=10)
    assert created.ok, created.combined
    try:
        result = inspect_prefix_path(remote_node(), str(junction / "missing" / "env"), runner)
        assert result.blocked
        assert "reparse" in result.reason or "link" in result.reason
        assert not (external / "missing").exists()
    finally:
        os.rmdir(junction)
