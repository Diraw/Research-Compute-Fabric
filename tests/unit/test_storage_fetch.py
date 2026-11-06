"""Only temporary files / fake SSH: no real output scanning or connections."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import local_node, make_home, posix_fs

from rcf.core.config import Config
from rcf.core.errors import Conflict, ExecutorError, RcfError
from rcf.core.ids import now_utc
from rcf.core.models import JobHandle, RunRecord
from rcf.core.runner import CommandResult, SubprocessRunner
from rcf.executors.factory import build_executor
from rcf.executors.local_exec import LocalExecutor
from rcf.executors.scripts import manifest_command
from rcf.runs.fetch import (
    ResultFetcher,
    SourceSet,
    _extract_verified,
    _prepare_destination,
    _safe_relative,
)
from rcf.storage.output_probe import local_output_manifest, parse_output_manifest


class Audit:
    """Minimal Store stand-in: audit sink plus an empty run index.

    The fetcher asks the store which runs already own a directory so a merge
    cannot overwrite another run's results; these fixtures own nothing.
    """

    def record_audit(self, **kwargs):
        self.last = kwargs

    def list_runs(self, *, limit: int = 50, state=None):
        return []


def record(root: Path, node: str = "laptop", *, custom: str | None = None) -> RunRecord:
    return RunRecord(
        run_id="run-test", node_id=node, executor="local" if node == "laptop" else "ssh",
        project_id="demo", state="succeeded", created_at=now_utc(),
        handle=JobHandle(kind="local" if node == "laptop" else "ssh", node_id=node, workdir=str(root)),
        result_dir=custom,
    )


def local_fetcher(tmp_path: Path) -> ResultFetcher:
    home = make_home(tmp_path, nodes='[nodes.laptop]\nkind = "local"\n')
    return ResultFetcher(Config.load(home), Audit(), SubprocessRunner())


def test_custom_outputs_over_200_complete_hashed_receipt(tmp_path):
    source = tmp_path / "run" / "custom outputs"
    source.mkdir(parents=True)
    for i in range(257):
        (source / f"file {i:03}.txt").write_text(f"content {i}", encoding="utf-8")
    ignored = source.parent / "outputs"
    ignored.mkdir()
    (ignored / "wrong").write_text("not custom")
    fetcher = local_fetcher(tmp_path)
    result = fetcher.fetch(record(source.parent, custom=str(source)), dest=tmp_path / "dest", confirmed=True)
    receipt = result["copy_receipt"]
    assert result["files"] == 257
    assert result["verified"] is True
    assert receipt["complete"] is receipt["verified"] is True
    assert receipt["source_root"] == str(source)
    assert Path(receipt["dest_root"]).is_absolute()
    assert receipt["dest_node"] == "controller"
    assert receipt["run_id"] == "run-test" and receipt["project_id"] == "demo"
    assert len(receipt["entries"]) == 257
    for entry in receipt["entries"]:
        data = (tmp_path / "dest" / entry["path"]).read_bytes()
        assert entry == {"path": entry["path"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert not (tmp_path / "dest" / "wrong").exists()


def test_fallback_outputs_and_explicit_overflow(tmp_path):
    source = tmp_path / "run" / "outputs"
    source.mkdir(parents=True)
    for name in ("a", "b", "c"):
        (source / name).write_text(name)
    fetcher = local_fetcher(tmp_path)
    with pytest.raises(ExecutorError, match="limit"):
        fetcher.fetch(record(source.parent), dest=tmp_path / "too-small", confirmed=True, limit=2)
    assert list((tmp_path / "too-small").iterdir()) == []
    result = fetcher.fetch(record(source.parent), dest=tmp_path / "ok", confirmed=True)
    assert result["files"] == 3
    executor = LocalExecutor(local_node(), tmp_path / "runs", SubprocessRunner())
    with pytest.raises(ExecutorError, match="limit"):
        executor.result_manifest(record(source.parent).handle, limit=2)


def test_local_same_size_corruption_is_not_verified(tmp_path, monkeypatch):
    import rcf.runs.fetch as module

    root = tmp_path / "source"
    root.mkdir()
    (root / "value").write_bytes(b"AAAA")
    source = SourceSet(str(root), local_output_manifest(root), "output_dir")
    dest = tmp_path / "dest"
    dest.mkdir()
    real_write = module._write_file

    def corrupt(stream, target):
        real_write(stream, target)
        target.write_bytes(b"BBBB")

    monkeypatch.setattr(module, "_write_file", corrupt)
    with pytest.raises(ExecutorError, match="checksum mismatch"):
        local_fetcher(tmp_path)._copy_local(source, dest)


def test_source_same_size_change_and_missing_are_errors(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    value = root / "value"
    value.write_bytes(b"AAAA")
    source = SourceSet(str(root), local_output_manifest(root), "output_dir")
    dest = tmp_path / "dest"
    dest.mkdir()
    fetcher = local_fetcher(tmp_path)
    value.write_bytes(b"BBBB")
    with pytest.raises(ExecutorError, match="changed after enumeration"):
        fetcher._copy_local(source, dest)
    value.unlink()
    with pytest.raises(FileNotFoundError):
        fetcher._copy_local(source, dest)


def symlink_or_skip(link: Path, target: Path, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlinks require OS permission")


def test_destination_symlink_and_source_symlink_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    value = source / "value"
    value.write_bytes(b"source")
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    symlink_or_skip(dest / "value", outside)
    fetcher = local_fetcher(tmp_path)
    manifest = local_output_manifest(source)
    with pytest.raises(Conflict, match="symlink/reparse"):
        fetcher._copy_local(SourceSet(str(source), manifest, "output_dir"), dest)
    assert outside.read_bytes() == b"outside"
    symlink_or_skip(source / "link", outside)
    with pytest.raises(ExecutorError, match="symlink/reparse"):
        local_output_manifest(source)
    directory_link = tmp_path / "dir-link"
    symlink_or_skip(directory_link, source, directory=True)
    with pytest.raises(Conflict, match="symlink/reparse"):
        _prepare_destination(directory_link / "child", force=True)


def test_reparse_target_rejected_even_without_symlink_privilege(tmp_path, monkeypatch):
    import rcf.runs.fetch as module

    dest = tmp_path / "junction"
    dest.mkdir()
    original = Path.lstat

    def fake_lstat(self, *args, **kwargs):
        st = original(self, *args, **kwargs)
        if self == dest:
            return SimpleNamespace(st_mode=st.st_mode, st_file_attributes=0x400)
        return st

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(Conflict, match="symlink/reparse"):
        module._prepare_destination(dest, force=True)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/drive", "a/../b", "file:stream", "a//b"])
def test_unsafe_manifest_paths(path):
    with pytest.raises(RcfError):
        _safe_relative(path)


def test_leading_dot_slash_is_normalized_not_traversal():
    # GNU find commonly prints ``./name``; it names the same relative file and
    # must not be confused with ``../`` traversal.
    assert _safe_relative("./file") == "file"
    assert _safe_relative("./a/b.txt") == "a/b.txt"


def test_archive_rejects_links_and_missing_extra_duplicates(tmp_path):
    for kind in ("symlink", "hardlink", "missing", "extra", "duplicate", "parent"):
        archive = tmp_path / f"{kind}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            if kind != "missing":
                info = tarfile.TarInfo("../escape" if kind == "parent" else "a")
                if kind in ("symlink", "hardlink"):
                    info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    info.linkname = "outside"
                    tar.addfile(info)
                else:
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"a"))
                    if kind in ("duplicate", "extra"):
                        info = tarfile.TarInfo("a" if kind == "duplicate" else "extra")
                        info.size = 1
                        tar.addfile(info, io.BytesIO(b"a"))
        dest = tmp_path / f"dest-{kind}"
        dest.mkdir()
        with pytest.raises(RcfError):
            _extract_verified(archive, dest, [{"path": "a", "size": 1}])
        assert not list(dest.iterdir())


def test_manifest_json_special_names_and_bad_probe(tmp_path, bash_binary):
    root = tmp_path / "special root"
    root.mkdir()
    names = ["space ' quote.txt", "unicode-研究.txt", "-leading.txt"]
    if os.name != "nt":
        names += ["newline\nfile", "tab\tfile", "literal\\name"]
    for name in names:
        (root / name).write_bytes(b"contents")
    import sys

    # Avoid Windows App Execution Alias python3 in this direct-shell test.
    prelude = "python3() { " + __import__("shlex").quote(posix_fs(sys.executable)) + ' "$@"; };\n'
    result = SubprocessRunner().run([bash_binary, "-s"], stdin=prelude + manifest_command(str(root)), timeout=30)
    entries = parse_output_manifest(result.stdout, returncode=result.returncode)
    assert {e["path"] for e in entries} == set(names)
    assert all(len(e["sha256"]) == 64 for e in entries)
    with pytest.raises(ExecutorError):
        parse_output_manifest('{"complete":false,"error":"permission denied"}')
    with pytest.raises(ExecutorError):
        parse_output_manifest("not json")


@pytest.mark.parametrize("kind", ["ssh", "slurm"])
def test_fake_ssh_custom_root_over_200_and_verified_receipt(slurm_home, ssh_sandbox, tmp_path, kind):
    config = Config.load(slurm_home)
    node = config.node("lab-hpc").model_copy(update={"kind": kind})
    # NodeKind is a StrEnum, factory accepts enum members rather than raw str.
    from rcf.core.models import NodeKind

    node = node.model_copy(update={"kind": NodeKind(kind)})
    run_dir = ssh_sandbox.run_dirs / "run-fetch"
    source = run_dir / "custom outputs"
    source.mkdir(parents=True)
    for i in range(205):
        (source / f"result {i}").write_bytes(str(i).encode())
    (source / "quote ' and 研究").write_bytes(b"special")
    executor = build_executor(config, node, SubprocessRunner())
    handle = JobHandle(kind=kind, node_id="lab-hpc", workdir=str(run_dir))
    entries = executor.result_manifest(handle, root=str(source), limit=300)
    assert len(entries) == 206
    with pytest.raises(ExecutorError, match="limit"):
        executor.result_manifest(handle, root=str(source), limit=200)
    # Exercise real SSH transport/fake host and fake scp, never a real server.
    fetcher = ResultFetcher(config, Audit(), SubprocessRunner())
    payload = fetcher.fetch(record(run_dir, "lab-hpc", custom=str(source)), dest=tmp_path / "download", confirmed=True)
    assert payload["files"] == 206
    assert payload["copy_receipt"]["verified"] is True
    assert payload["copy_receipt"]["complete"] is True
    assert not list(run_dir.glob(".rcf-fetch-*.tar.gz"))


def test_bsd_shasum_fallback_uses_stdin(tmp_path, bash_binary):
    archive = tmp_path / "odd ' archive.tar.gz"
    archive.write_bytes(b"hash me")
    expected = hashlib.sha256(b"hash me").hexdigest()

    class BSDTransport:
        def run(self, command, **kwargs):
            # Model BSD tool discovery while keeping the real shell parser.
            prelude = (
                "command() { if [ \"$1\" = -v ]; then "
                "case \"$2\" in sha256sum) return 1;; shasum) return 0;; esac; fi; builtin command \"$@\"; };\n"
                "shasum() { [ \"$1\" = -a ] && [ \"$2\" = 256 ] || return 4; sha256sum; };\n"
            )
            return SubprocessRunner().run([bash_binary, "-s"], stdin=prelude + command)

    assert local_fetcher(tmp_path)._remote_sha256(BSDTransport(), str(archive)) == expected


def test_missing_remote_hash_is_not_copy_evidence(tmp_path, monkeypatch):
    import rcf.runs.fetch as module

    root = tmp_path / "source"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    entries = local_output_manifest(root)
    source = SourceSet(str(root), entries, "output_dir")
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root / "a", arcname="a")

    class FakeTransport:
        def run(self, command, **kwargs):
            return CommandResult((), 0, "")

        def fetch_file(self, remote, local):
            shutil.copyfile(archive, local)

    monkeypatch.setattr(module.SshTransport, "from_node", lambda *args: FakeTransport())
    dest = tmp_path / "dest"
    dest.mkdir()
    result = local_fetcher(tmp_path)._copy_remote(SimpleNamespace(id="fake"), None, record(root), source, dest)
    assert result["files"] == 1 and result["verified"] is False
    assert result["entries"][0]["sha256"] == hashlib.sha256(b"a").hexdigest()


def test_json_protocol_escapes_newlines_and_tabs():
    raw = {"complete": True, "entries": [{"path": "line\n\ttwo", "size": 0}]}
    assert parse_output_manifest(json.dumps(raw))[0]["path"] == "line\n\ttwo"
