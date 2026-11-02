"""Real OS exclusion and no-follow paths for controller execution locks."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from rcf.core.errors import Conflict, UnsupportedOperation
from rcf.storage import execution
from rcf.storage.execution import execution_lock

PLAN = "execution-lock-test"


def lock_path(home, plan=PLAN):
    return home / "state" / "storage-executions" / (hashlib.sha256(plan.encode()).hexdigest() + ".lock")


def child(script, home):
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    return subprocess.run([sys.executable, "-c", script, str(home)], capture_output=True,
                          text=True, timeout=20, **kwargs)


def symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlink capability unavailable: {exc}")


def junction(link, target):
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True, timeout=10, creationflags=0x08000000)
    assert result.returncode == 0, result.stdout + result.stderr


def test_owner_protocol_and_lock_scope_remain_stable(tmp_path):
    with execution_lock(tmp_path, PLAN) as first:
        assert {"controller", "pid", "process_instance", "process_observed_at", "claimed_at",
                "heartbeat_at", "stage", "token", "lock_scope"} <= first.keys()
        assert first["pid"] == os.getpid()
        scope = str(lock_path(tmp_path).absolute())
        if os.name == "nt":
            scope = scope.casefold()
        assert first["lock_scope"] == hashlib.sha256(scope.encode()).hexdigest()
        with execution_lock(tmp_path, "independent-plan") as other:
            assert first["lock_scope"] != other["lock_scope"]
    with execution_lock(tmp_path, PLAN) as second:
        assert first["token"] != second["token"]
        assert first["process_instance"] == second["process_instance"]
        assert first["lock_scope"] == second["lock_scope"]
    assert lock_path(tmp_path).read_bytes() == b"0"


def test_same_thread_and_cross_thread_contenders_reach_os_lock(tmp_path):
    entered, release = Barrier(2), Barrier(2)

    def own():
        with execution_lock(tmp_path, PLAN):
            with pytest.raises(Conflict, match="another live process"):
                with execution_lock(tmp_path, PLAN):
                    pytest.fail("reentrant lock succeeded")
            entered.wait(timeout=10)
            release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(own)
        entered.wait(timeout=10)
        try:
            with pytest.raises(Conflict, match="another live process"):
                with execution_lock(tmp_path, PLAN):
                    pytest.fail("two threads own the same plan")
        finally:
            release.wait(timeout=10)
        future.result(timeout=10)


def test_live_process_contention_and_release(tmp_path):
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from rcf.storage.execution import execution_lock\n"
        "from rcf.core.errors import Conflict\n"
        "try:\n"
        f" with execution_lock(Path(sys.argv[1]), {PLAN!r}):\n  print('entered')\n"
        "except Conflict as exc:\n print(str(exc)); sys.exit(23)\n"
    )
    with execution_lock(tmp_path, PLAN):
        result = child(script, tmp_path)
        assert result.returncode == 23, result.stderr
        assert "another live process" in result.stdout
    result = child(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "entered"


def test_abrupt_process_death_releases_lock(tmp_path):
    script = (
        "import os,sys\nfrom pathlib import Path\n"
        "from rcf.storage.execution import execution_lock\n"
        f"with execution_lock(Path(sys.argv[1]), {PLAN!r}) as owner:\n"
        " print(owner['token'], flush=True)\n os._exit(17)\n"
    )
    result = child(script, tmp_path)
    assert result.returncode == 17, result.stderr
    assert result.stdout.strip().startswith("execution_")
    with execution_lock(tmp_path, PLAN) as owner:
        assert owner["token"] != result.stdout.strip()


def test_body_error_is_not_disguised_as_path_conflict_and_releases(tmp_path):
    with pytest.raises(OSError, match="body failure"):
        with execution_lock(tmp_path, PLAN):
            raise OSError("body failure")
    with execution_lock(tmp_path, PLAN):
        pass


def test_hardlinked_lock_file_is_rejected_without_writing_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"do-not-touch")
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    os.link(target, path)
    with pytest.raises(Conflict, match="unsafe"):
        with execution_lock(tmp_path, PLAN):
            pytest.fail("hardlink accepted")
    assert target.read_bytes() == b"do-not-touch"


def test_symlink_lock_file_is_rejected_without_writing_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"do-not-touch")
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    symlink_or_skip(path, target)
    with pytest.raises(Conflict):
        with execution_lock(tmp_path, PLAN):
            pytest.fail("symlink accepted")
    assert target.read_bytes() == b"do-not-touch"


def test_directory_in_place_of_lock_file_is_rejected(tmp_path):
    lock_path(tmp_path).mkdir(parents=True)
    with pytest.raises(Conflict):
        with execution_lock(tmp_path, PLAN):
            pytest.fail("directory accepted")


@pytest.mark.parametrize("component", ["home", "ancestor", "state", "storage-executions"])
def test_symlink_directory_components_are_rejected(tmp_path, component):
    target = tmp_path / "outside"
    target.mkdir()
    if component == "home":
        home = tmp_path / "home"
        link = home
    elif component == "ancestor":
        link = tmp_path / "ancestor"
        home = link / "home"
    else:
        home = tmp_path / "home"
        link = home / "state"
        if component == "storage-executions":
            link = link / component
        link.parent.mkdir(parents=True)
    symlink_or_skip(link, target, directory=True)
    with pytest.raises(Conflict):
        with execution_lock(home, PLAN):
            pytest.fail("linked directory accepted")
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and handle semantics")
@pytest.mark.parametrize("component", ["home", "ancestor", "state", "storage-executions"])
def test_junction_directory_components_are_rejected(tmp_path, component):
    target = tmp_path / "outside"
    target.mkdir()
    if component == "home":
        home = tmp_path / "home"
        link = home
    elif component == "ancestor":
        link = tmp_path / "ancestor"
        home = link / "home"
    else:
        home = tmp_path / "home"
        link = home / "state"
        if component == "storage-executions":
            link = link / component
        link.parent.mkdir(parents=True)
    junction(link, target)
    try:
        with pytest.raises(Conflict):
            with execution_lock(home, PLAN):
                pytest.fail("junction accepted")
        assert list(target.iterdir()) == []
    finally:
        link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and handle semantics")
def test_ancestors_cannot_be_replaced_between_validation_and_file_open(tmp_path, monkeypatch):
    home = tmp_path / "ancestor" / "home"
    original = execution._WindowsFiles.open
    attempted = []

    def race(api, path, *, directory, create=False):
        if not directory and create:
            for victim in (home.parent, home, home / "state", home / "state" / "storage-executions"):
                with pytest.raises(OSError) as caught:
                    victim.rename(victim.with_name(victim.name + "-moved"))
                assert caught.value.winerror in {5, 32}
                attempted.append(victim)
        return original(api, path, directory=directory, create=create)

    monkeypatch.setattr(execution._WindowsFiles, "open", race)
    with execution_lock(home, PLAN):
        assert len(attempted) == 4
    # No leaked directory handle after exit.
    home.rename(home.with_name("released"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing and handle semantics")
def test_lock_file_cannot_be_renamed_and_ancestor_cannot_be_reparse_modified(tmp_path):
    home = tmp_path / "home"
    with execution_lock(home, PLAN):
        with pytest.raises(OSError) as caught:
            lock_path(home).rename(lock_path(home).with_suffix(".moved"))
        assert caught.value.winerror == 32
        api = execution._WindowsFiles()
        # Setting a junction/reparse point in-place requires a write-capable
        # directory handle. Denying only DELETE sharing would not prevent that.
        handle = api.kernel.CreateFileW(str(home), 0x40000000, 0x7, None, 3,
                                        0x00200000 | 0x02000000, None)
        if handle != api.invalid:
            api.close(handle)
            pytest.fail("write-capable ancestor handle allowed during execution")
        assert api.ctypes.get_last_error() == 32
    lock_path(home).rename(lock_path(home).with_suffix(".moved"))


@pytest.mark.skipif(os.name != "nt", reason="Windows race regression")
def test_junction_swap_before_directory_open_is_blocked_or_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = execution._WindowsFiles.open
    swapped = attempted = False

    def race(api, path, *, directory, create=False):
        nonlocal swapped, attempted
        if directory and path == home and not attempted:
            attempted = True
            try:
                home.rename(tmp_path / "original-home")
            except OSError as exc:
                # A pinned parent can already deny the replacement operation.
                assert exc.winerror in {5, 32}
            else:
                junction(home, outside)
                swapped = True
        return original(api, path, directory=directory, create=create)

    monkeypatch.setattr(execution._WindowsFiles, "open", race)
    try:
        try:
            with execution_lock(home, PLAN):
                assert not swapped, "racing junction accepted"
        except Conflict:
            assert swapped
        assert attempted
        assert list(outside.iterdir()) == []
    finally:
        if swapped:
            home.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows race regression")
def test_final_symlink_swapped_in_at_open_is_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    probe = tmp_path / "probe-link"
    symlink_or_skip(probe, target)
    probe.unlink()
    original = execution._WindowsFiles.open

    def race(api, path, *, directory, create=False):
        if not directory and create:
            path.symlink_to(target)
        return original(api, path, directory=directory, create=create)

    monkeypatch.setattr(execution._WindowsFiles, "open", race)
    with pytest.raises(Conflict):
        with execution_lock(home, PLAN):
            pytest.fail("racing final symlink accepted")
    assert target.read_bytes() == b"untouched"


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow capabilities")
def test_missing_nofollow_capability_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(UnsupportedOperation, match="O_NOFOLLOW"):
        with execution_lock(tmp_path, PLAN):
            pytest.fail("unsafe fallback used")
    assert not (tmp_path / "state").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd traversal")
def test_posix_opens_each_component_relative_without_following(tmp_path, monkeypatch):
    original_open, original_mkdir = os.open, os.mkdir
    opens, mkdirs = [], []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        opens.append((path, flags, dir_fd))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def tracked_mkdir(path, mode=0o777, *, dir_fd=None):
        mkdirs.append((path, dir_fd))
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "mkdir", tracked_mkdir)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {tracked_open, tracked_mkdir})
    with execution_lock(tmp_path, PLAN):
        assert opens[0][0] == "/"
        for path, flags, parent in opens[1:]:
            assert "/" not in str(path)
            assert flags & os.O_NOFOLLOW
            assert parent is not None
        assert all(parent is not None for _, parent in mkdirs)
        assert [name for name, _ in mkdirs[-2:]] == ["state", "storage-executions"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO semantics")
def test_fifo_lock_file_is_rejected_without_blocking(tmp_path):
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    with pytest.raises(Conflict, match="unsafe"):
        with execution_lock(tmp_path, PLAN):
            pytest.fail("FIFO accepted")


def test_parent_traversal_is_rejected_before_creation(tmp_path):
    home = tmp_path / "uncreated" / ".." / "home"
    with pytest.raises(Conflict, match="parent traversal"):
        with execution_lock(home, PLAN):
            pytest.fail("parent traversal accepted")
    assert not (tmp_path / "uncreated").exists()
