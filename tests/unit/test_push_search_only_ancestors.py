"""Search-only ancestors must work without weakening fd-anchored extraction."""
from __future__ import annotations

import errno
import io
import os
import stat
import sys
import tarfile
from types import SimpleNamespace

import pytest

from rcf.projects.push import _SAFE_EXTRACT


def simulated_posix(*, path_handles=True, denied=None):
    """Exercise flag selection on Windows too; real permissions tested below."""
    records = []
    closed = []
    fake = SimpleNamespace(name="posix", O_RDONLY=0, O_DIRECTORY=0x10000, O_NOFOLLOW=0x20000)
    if path_handles:
        fake.O_PATH = 0x40000

    def opened(name, flags, **kwargs):
        records.append((name, flags, kwargs))
        if name == denied:
            raise PermissionError(errno.EACCES, "Permission denied", name)
        # Represents a search-only ancestor: O_RDONLY cannot list/open it.
        if name == "home" and path_handles and not flags & fake.O_PATH:
            raise PermissionError(errno.EACCES, "Permission denied", name)
        return len(records) + 10

    def mkdir(*_args, **_kwargs):
        raise FileExistsError(errno.EEXIST, "already exists")

    fake.open = opened
    fake.mkdir = mkdir
    fake.close = closed.append
    fake.fstat = lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR | 0o711)
    namespace = {}
    exec(_SAFE_EXTRACT, namespace)
    namespace["os"] = fake
    return namespace, fake, records, closed


def test_path_handles_traverse_search_only_ancestor_without_read_permission():
    ns, fake, records, closed = simulated_posix()
    parent, descriptors = ns["directory"]("/public/home/user/target", ["nested"])
    assert parent == descriptors[-1]
    assert [x[0] for x in records] == ["/", "public", "home", "user", "target", "nested"]
    assert all(flags == fake.O_PATH | fake.O_DIRECTORY | fake.O_NOFOLLOW for _, flags, _ in records)
    assert all(row[2]["dir_fd"] == descriptors[i - 1] for i, row in enumerate(records) if i)
    for fd in reversed(descriptors):
        fake.close(fd)
    assert closed == list(reversed(descriptors))


def test_no_o_path_platform_retains_strict_readonly_fallback():
    ns, fake, records, _ = simulated_posix(path_handles=False)
    ns["directory"]("/readable/root", [])
    assert all(flags == fake.O_RDONLY | fake.O_DIRECTORY | fake.O_NOFOLLOW for _, flags, _ in records)


def test_permissions_remain_denied_and_descriptors_are_closed():
    ns, _, records, closed = simulated_posix(denied="target")
    with pytest.raises(ValueError, match="(?i)(permission|access denied)"):
        ns["directory"]("/public/home/user/target", [])
    assert sorted(closed) == list(range(11, 11 + len(records) - 1))


def test_opened_handle_is_verified_as_directory():
    ns, fake, _, closed = simulated_posix()
    fake.fstat = lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o600)
    with pytest.raises(ValueError, match="directory"):
        ns["directory"]("/target", [])
    assert closed  # failed verification must not leak even the first handle


def make_archive(path, content=b"new"):
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("nested/value")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))


requires_linux_user = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "O_PATH") or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="requires Linux O_PATH and non-root effective user for real search-only permissions",
)


@requires_linux_user
def test_real_nonroot_search_only_ancestor_extracts_and_replaces(tmp_path):
    ancestor = tmp_path / "private-parent"
    target = ancestor / "user" / "target"
    target.mkdir(parents=True)
    archive = tmp_path / "payload.tar.gz"
    make_archive(archive)
    # Owner search-only bits reproduce the non-owner search bits on /public/home 0711.
    ancestor.chmod(0o111)
    ns = {}
    exec(_SAFE_EXTRACT, ns)
    try:
        with pytest.raises(PermissionError):
            os.listdir(ancestor)
        with pytest.raises(PermissionError):
            os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        ns["extract"](str(target), str(archive), ["nested/value"])
        assert (target / "nested/value").read_bytes() == b"new"
        make_archive(archive, b"replacement")
        ns["extract"](str(target), str(archive), ["nested/value"])
        assert (target / "nested/value").read_bytes() == b"replacement"
    finally:
        ancestor.chmod(0o700)


@requires_linux_user
def test_real_nonroot_write_denial_is_not_bypassed(tmp_path):
    target = tmp_path / "readonly"
    target.mkdir()
    archive = tmp_path / "payload.tar.gz"
    make_archive(archive)
    target.chmod(0o500)
    ns = {}
    exec(_SAFE_EXTRACT, ns)
    try:
        with pytest.raises(PermissionError):
            ns["extract"](str(target), str(archive), ["nested/value"])
        assert not (target / "nested").exists()
    finally:
        target.chmod(0o700)
