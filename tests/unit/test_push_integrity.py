"""Push integrity failures are closed; fixtures never contact a real host."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from types import SimpleNamespace

import pytest
from conftest import local_node, posix_fs, ssh_node

from rcf.core.config import Config
from rcf.core.errors import RcfError
from rcf.core.models import ProbeStatus, ProjectSpec
from rcf.core.runner import CommandResult, ScriptedRunner, SubprocessRunner
from rcf.projects.push import ProjectPusher


@pytest.fixture
def pusher(tmp_path):
    return ProjectPusher(SimpleNamespace(home=tmp_path), ScriptedRunner([]))


def project(source, target):
    return ProjectSpec(id="demo", paths={"source": str(source), "target": str(target)})


def same_metadata_files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for root, content in ((source, b"AAAA"), (target, b"BBBB")):
        (root / "value").write_bytes(content)
        os.utime(root / "value", (1700000000, 1700000000))
    return source, target


def test_same_size_and_exact_mtime_requires_content_hash(pusher, tmp_path):
    source, target = same_metadata_files(tmp_path)
    plan = pusher.plan(project(source, target), local_node("target"), source=local_node("source"))
    assert [entry.path for entry in plan.changed] == ["value"]
    assert plan.unchanged == 0


@pytest.mark.parametrize("hashes", [{}, {"value": None}, {"value": ""}, {"value": "invalid"}])
def test_missing_or_invalid_hash_cannot_be_unchanged(pusher, tmp_path, monkeypatch, hashes):
    source, target = same_metadata_files(tmp_path)
    monkeypatch.setattr(pusher, "_hash_local", lambda *args: hashes)
    with pytest.raises(RcfError, match="hash"):
        pusher.plan(project(source, target), local_node("target"), source=local_node("source"))


@pytest.mark.parametrize("result", [
    CommandResult((), 1, stdout="a" * 64 + "  value\n", stderr="read failed"),
    CommandResult((), 0, stdout=""),
    CommandResult((), 0, stdout="a" * 64 + "  other\n"),
    CommandResult((), 0, stdout="a" * 64 + "  value\n" + "a" * 64 + "  extra\n"),
    CommandResult((), 0, stdout="a" * 64 + "  value\n" + "a" * 64 + "  value\n"),
    CommandResult((), 0, stdout="not-a-digest  value\n"),
    CommandResult((), 0, stdout="a" * 64 + "  value\n", timed_out=True),
])
def test_remote_hash_rejects_failure_or_incomplete_set(pusher, monkeypatch, result):
    from rcf.executors.ssh import SshTransport

    monkeypatch.setattr(SshTransport, "run", lambda *args, **kwargs: result)
    with pytest.raises(RcfError, match="hash"):
        pusher._hash_remote(ssh_node(), "/project", ["value"])


def test_remote_hash_batches_and_quotes(pusher, monkeypatch):
    from rcf.executors.ssh import SshTransport

    paths = [f"file {index:03}'x" for index in range(260)]
    calls = []
    digest = hashlib.sha256(b"data").hexdigest()

    def run(self, command, **kwargs):
        calls.append(command)
        selected = paths[(len(calls) - 1) * 128:len(calls) * 128]
        return CommandResult((), 0, stdout="".join(f"{digest}  ./{p}\n" for p in selected))

    monkeypatch.setattr(SshTransport, "run", run)
    assert pusher._hash_remote(ssh_node(), "/project with spaces", paths) == dict.fromkeys(paths, digest)
    assert len(calls) == 3
    assert all("cd '/project with spaces'" in command for command in calls)
    assert "'\"'\"'" in calls[0]


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "root", "ancestor"])
def test_local_scan_rejects_links_without_reading_external_files(pusher, tmp_path, kind):
    root = tmp_path / "source"
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret").write_bytes(b"outside")
    if kind == "root":
        symlink(root, external, directory=True)
    elif kind == "ancestor":
        link = tmp_path / "parent"
        symlink(link, external, directory=True)
        (external / "nested").mkdir()
        root = link / "nested"
    else:
        root.mkdir()
        destination = external if kind == "directory" else external / ("missing" if kind == "dangling" else "secret")
        symlink(root / "link", destination, directory=kind == "directory")
    with pytest.raises(RcfError, match="(?i)link"):
        pusher._scan_local(root, [])


def test_local_scan_rejects_special_file(pusher, tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    item = root / "fifo"
    item.write_bytes(b"")
    original = os.stat_result
    real_lstat = type(item).lstat

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if path == item:
            return original((stat.S_IFIFO | 0o600, *info[1:]))
        return info

    monkeypatch.setattr(type(item), "lstat", lstat)
    with pytest.raises(RcfError, match="(?i)(special|regular)"):
        pusher._scan_local(root, [])


@pytest.mark.parametrize("operation", ["hash", "pack"])
def test_local_file_replaced_by_link_after_scan_is_rejected(pusher, tmp_path, operation):
    root = tmp_path / "source"
    root.mkdir()
    item = root / "value"
    item.write_bytes(b"original")
    assert pusher._scan_local(root, []) == {"value": (8, item.stat().st_mtime)}
    item.unlink()
    external = tmp_path / "secret"
    external.write_bytes(b"external")
    symlink(item, external)
    with pytest.raises(RcfError, match="(?i)link"):
        if operation == "hash":
            pusher._hash_local(root, ["value"])
        else:
            pusher._pack_local(root, ["value"], tmp_path / "archive.tar.gz")


@pytest.mark.parametrize("hashes", [{}, {"value": None}, {"value": ""}])
def test_apply_rejects_incomplete_packing_hashes(pusher, tmp_path, monkeypatch, hashes):
    source = tmp_path / "source"
    source.mkdir()
    (source / "value").write_bytes(b"data")
    target = tmp_path / "target"
    spec = project(source, target)
    plan = pusher.plan(spec, local_node("target"), source=local_node("source"))

    def pack(*args):
        pusher._pack_local(source, ["value"], args[-1])
        return hashes

    monkeypatch.setattr(pusher, "_pack_file", pack)
    with pytest.raises(RcfError, match="hash"):
        pusher.apply(spec, local_node("target"), plan, source=local_node("source"))
    assert not (target / "value").exists()


def test_remote_pack_rejects_empty_hash_result(pusher, tmp_path, monkeypatch):
    from rcf.executors.ssh import SshTransport

    monkeypatch.setattr(pusher, "_remote_tmp", lambda node: "/tmp/staging")
    monkeypatch.setattr(SshTransport, "run", lambda *args, **kwargs: CommandResult((), 0))

    def fetch(self, remote, archive, **kwargs):
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("value")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"data"))

    monkeypatch.setattr(SshTransport, "fetch_file", fetch)
    monkeypatch.setattr(pusher, "_hash_remote", lambda *args: {})
    with pytest.raises(RcfError, match="hash"):
        pusher._pack_remote(ssh_node(), "/source", ["value"], tmp_path / "archive.tar.gz")


@pytest.mark.parametrize("member_kind", ["symlink", "hardlink", "directory", "extra", "missing", "corrupt"])
def test_remote_pack_validates_members_and_bytes(pusher, tmp_path, monkeypatch, member_kind):
    from rcf.executors.ssh import SshTransport

    monkeypatch.setattr(pusher, "_remote_tmp", lambda node: "/tmp/staging")
    monkeypatch.setattr(SshTransport, "run", lambda *args, **kwargs: CommandResult((), 0))
    digest = hashlib.sha256(b"data").hexdigest()
    monkeypatch.setattr(pusher, "_hash_remote", lambda *args: {"value": digest})

    def fetch(self, remote, archive, **kwargs):
        with tarfile.open(archive, "w:gz") as tar:
            if member_kind == "missing":
                return
            info = tarfile.TarInfo("extra" if member_kind == "extra" else "value")
            if member_kind in {"symlink", "hardlink", "directory"}:
                info.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                             "directory": tarfile.DIRTYPE}[member_kind]
                info.linkname = "../secret"
                tar.addfile(info)
            else:
                info.size = 4
                tar.addfile(info, io.BytesIO(b"oops" if member_kind == "corrupt" else b"data"))

    monkeypatch.setattr(SshTransport, "fetch_file", fetch)
    with pytest.raises(RcfError, match="(?i)(archive|hash)"):
        pusher._pack_remote(ssh_node(), "/source", ["value"], tmp_path / "archive.tar.gz")


def test_remote_pack_accepts_complete_regular_archive(pusher, tmp_path, monkeypatch):
    from rcf.executors.ssh import SshTransport

    monkeypatch.setattr(pusher, "_remote_tmp", lambda node: "/tmp/staging")
    monkeypatch.setattr(SshTransport, "run", lambda *args, **kwargs: CommandResult((), 0))
    expected = {"value": hashlib.sha256(b"data").hexdigest()}
    monkeypatch.setattr(pusher, "_hash_remote", lambda *args: expected)

    def fetch(self, remote, archive, **kwargs):
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("./value")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"data"))

    monkeypatch.setattr(SshTransport, "fetch_file", fetch)
    assert pusher._pack_remote(ssh_node(), "/source", ["value"], tmp_path / "archive.tar.gz") == expected


@pytest.mark.parametrize("kind", ["file", "directory", "root"])
def test_target_link_introduced_after_plan_is_rejected(pusher, tmp_path, kind):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "value").write_bytes(b"data")
    target = tmp_path / "target"
    spec = project(source, target)
    plan = pusher.plan(spec, local_node("target"), source=local_node("source"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "value").write_bytes(b"safe")
    if kind == "root":
        symlink(target, outside, directory=True)
    else:
        target.mkdir()
        if kind == "directory":
            symlink(target / "nested", outside, directory=True)
        else:
            (target / "nested").mkdir()
            symlink(target / "nested" / "value", outside / "value")
    with pytest.raises(RcfError, match="(?i)link"):
        pusher.apply(spec, local_node("target"), plan, source=local_node("source"))
    assert (outside / "value").read_bytes() == b"safe"


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "C:/absolute", "..\\escape", "."])
def test_unsafe_paths_are_rejected_before_hash_or_pack(pusher, tmp_path, relative):
    with pytest.raises(RcfError, match="unsafe"):
        pusher._hash_local(tmp_path, [relative])
    with pytest.raises(RcfError, match="unsafe"):
        pusher._pack_local(tmp_path, [relative], tmp_path / "archive.tar.gz")


def test_remote_hash_and_pack_with_real_bash_fake_ssh(slurm_home, ssh_sandbox, tmp_path):
    config = Config.load(slurm_home)
    node = config.nodes["lab-hpc"]
    remote = ProjectPusher(config, SubprocessRunner())
    root = ssh_sandbox.host / "push source"
    root.mkdir()
    paths = [f"file {index:03}'quoted" for index in range(129)]
    expected = {}
    for relative in paths:
        data = relative.encode()
        (root / relative).write_bytes(data)
        expected[relative] = hashlib.sha256(data).hexdigest()
    assert remote._hash_remote(node, posix_fs(root), paths) == expected
    archive = tmp_path / "archive.tar.gz"
    assert remote._pack_remote(node, posix_fs(root), paths, archive) == expected
    with tarfile.open(archive, "r:gz") as tar:
        assert {entry.name.removeprefix("./") for entry in tar} == set(paths)
    with pytest.raises(RcfError, match="hash"):
        remote._hash_remote(node, posix_fs(root), [paths[0], "missing"])


def test_remote_newer_protection_precedes_hash(pusher, tmp_path, monkeypatch):
    source, target = same_metadata_files(tmp_path)
    os.utime(target / "value", (1700003600, 1700003600))
    monkeypatch.setattr(pusher, "_hash_local", lambda *args: pytest.fail("protected files must not hash"))
    plan = pusher.plan(project(source, target), local_node("target"), source=local_node("source"))
    assert [entry.path for entry in plan.remote_newer] == ["value"]
    assert plan.changed == []
    assert all(check.status is not ProbeStatus.FAIL for check in plan.checks)
