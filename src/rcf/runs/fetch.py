"""Complete bounded output copying with source/destination SHA-256 receipts.

Remote data still travels as one archive and one scp (including WSL scp path
mapping). Missing GNU/BSD archive hashes allow downloads, not copy evidence.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.errors import Conflict, ExecutorError, NotFound, RcfError, UnsupportedOperation
from ..core.ids import iso, now_utc
from ..core.models import NodeKind, RunRecord
from ..core.runner import Runner, shquote
from ..executors.factory import build_executor
from ..executors.ssh import SshTransport
from ..security.policy import SecurityPolicy
from ..security.redaction import redact
from ..storage.output_probe import output_command
from .store import Store

ARCHIVE_NAME = "rcf-fetch.tar.gz"
DEFAULT_FETCH_LIMIT = 100_000


@dataclass
class SourceSet:
    root: str
    entries: list[dict[str, Any]]
    origin: str


class ResultFetcher:
    def __init__(self, config: Config, store: Store, runner: Runner) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self.policy: SecurityPolicy = config.policy()

    def fetch(
        self, record: RunRecord, *, dest: Path, force: bool = False,
        confirmed: bool = False, actor: str = "cli", limit: int = DEFAULT_FETCH_LIMIT,
    ) -> dict[str, Any]:
        self.policy.check("run.fetch")
        self.policy.check_confirmation("run.fetch", confirmed)
        if record.handle is None:
            raise UnsupportedOperation(
                f"run {record.run_id!r} was never submitted; nothing to fetch",
                detail={"run_id": record.run_id},
            )
        if limit < 1:
            raise RcfError("fetch limit must be positive")
        # Do not resolve: that would conceal a symlink/reparse parent.
        dest = Path(os.path.abspath(dest.expanduser()))
        _prepare_destination(dest, force=force)
        node = self.config.node(record.node_id)
        executor = build_executor(self.config, node, self.runner)
        source = self._resolve_source(executor, record, limit=limit)
        _manifest_paths(source.entries)
        try:
            if node.kind is NodeKind.LOCAL:
                result = self._copy_local(source, dest)
            else:
                result = self._copy_remote(node, executor, record, source, dest)
            # Detect additions/removals and same-size rewrites during transfer.
            if source.origin == "output_dir":
                final_entries = executor.result_manifest(
                    record.handle, root=source.root, limit=limit + 1, hashes=True,
                )
            else:
                final_entries = executor.workdir_changes(record.handle, root=source.root, limit=limit + 1)
            if _fingerprints(final_entries) != _fingerprints(source.entries):
                raise ExecutorError("source output set changed during fetch; no complete copy receipt")
            for entry in result["entries"]:
                target = dest.joinpath(*entry["path"].split("/"))
                if target.lstat().st_size != entry["bytes"] or _sha256_file(target) != entry["sha256"]:
                    raise ExecutorError("destination changed during fetch; no verified copy receipt")
        except (OSError, tarfile.TarError) as exc:
            raise ExecutorError(f"cannot safely fetch outputs: {exc}") from exc
        created_at = iso(now_utc())
        receipt_entries = result.pop("entries")
        result.update({
            "run_id": record.run_id, "node_id": record.node_id, "dest": str(dest),
            "source": source.origin, "source_root": source.root, "fetched_at": created_at,
            "copy_receipt": {
                "source_node": record.node_id, "source_root": source.root,
                # The main storage layer may identify a configured local root.
                "dest_node": "controller", "dest_root": str(dest),
                "entries": receipt_entries, "complete": bool(result["verified"]),
                "verified": bool(result["verified"]), "run_id": record.run_id,
                "project_id": record.project_id, "created_at": created_at,
            },
        })
        self.store.record_audit(
            actor=actor, operation="run.fetch", tier="execute", decision="allowed",
            node_id=record.node_id, run_id=record.run_id,
            detail={"dest": str(dest), "files": result["files"], "bytes": result["bytes"],
                    "verified": result["verified"], "source": source.origin},
        )
        return result

    def _resolve_source(self, executor, record: RunRecord, *, limit: int) -> SourceSet:
        root = record.result_dir or posixpath.join(record.handle.workdir or "", "outputs")
        manifest_fn = getattr(executor, "result_manifest", None)
        # Probe one extra entry so a legacy truncating executor cannot silently
        # turn a prefix at the caller's limit into a complete receipt.
        entries = (manifest_fn(record.handle, limit=limit + 1, root=root, hashes=True)
                   if callable(manifest_fn) else [])
        if len(entries) > limit:
            raise ExecutorError(f"output manifest exceeds fetch limit {limit}")
        if entries:
            return SourceSet(root=_listed_root(entries, root), entries=entries, origin="output_dir")
        changes_fn = getattr(executor, "workdir_changes", None)
        changes = (
            changes_fn(record.handle, limit=limit + 1, root=record.workdir)
            if callable(changes_fn) else []
        )
        if len(changes) > limit:
            raise ExecutorError(f"project output manifest exceeds fetch limit {limit}")
        if changes:
            if not record.workdir:
                raise UnsupportedOperation("run has no project directory recorded")
            return SourceSet(root=_listed_root(changes, record.workdir), entries=changes, origin="project_dir")
        raise NotFound(
            f"run {record.run_id!r} produced no files RCF can see",
            hint="point the job at $RCF_OUTPUT_DIR; its output directory may have been removed",
            detail={"run_id": record.run_id, "result_dir": record.result_dir},
        )

    def _copy_local(self, source: SourceSet, dest: Path) -> dict[str, Any]:
        root = Path(os.path.abspath(Path(source.root).expanduser()))
        _check_chain(root)
        if dest == root or dest.is_relative_to(root) or root.is_relative_to(dest):
            raise Conflict("source and destination output trees must not overlap")
        entries = []
        for entry in source.entries:
            relative = _safe_relative(entry["path"])
            src = root.joinpath(*relative.split("/"))
            source_hash = _sha256_file(src)
            source_size = src.lstat().st_size
            if source_size != entry["size"] or (entry.get("sha256") and entry["sha256"] != source_hash):
                raise ExecutorError(f"source changed after enumeration: {relative!r}")
            target = _target_path(dest, relative)
            with _open_regular(src) as stream:
                _write_file(stream, target)
            target_hash = _sha256_file(target)
            if _sha256_file(src) != source_hash or target_hash != source_hash:
                raise ExecutorError(f"copied file checksum mismatch: {relative!r}")
            if target.lstat().st_size != source_size:
                raise ExecutorError(f"copied file size mismatch: {relative!r}")
            entries.append({"path": relative, "bytes": source_size, "sha256": target_hash})
        return {"files": len(entries), "bytes": sum(e["bytes"] for e in entries),
                "verified": True, "sha256": None, "entries": entries}

    def _copy_remote(self, node, executor, record: RunRecord, source: SourceSet, dest: Path) -> dict[str, Any]:
        transport = SshTransport.from_node(node, self.runner)
        run_dir = (record.handle.workdir if record.handle else "") or ""
        archive = posixpath.join(run_dir, f".rcf-fetch-{uuid.uuid4().hex}.tar.gz")
        try:
            packed = transport.run(
                output_command(source.root, archive=archive, entries=source.entries), timeout=600
            )
            if packed.returncode != 0:
                raise ExecutorError(
                    f"cannot pack results on {node.id!r}: {redact(packed.first_error_line())}",
                    detail={"stderr": redact(packed.stderr)[:400]},
                )
            remote_hash = self._remote_sha256(transport, archive)
            with tempfile.TemporaryDirectory(prefix="rcf-fetch-") as tmp:
                local_archive = Path(tmp) / ARCHIVE_NAME
                transport.fetch_file(archive, local_archive)
                local_hash = _sha256_file(local_archive)
                if remote_hash and local_hash != remote_hash:
                    raise ExecutorError("transferred archive does not match its checksum",
                                        detail={"expected": remote_hash, "actual": local_hash})
                entries = _extract_verified(local_archive, dest, source.entries)
        finally:
            transport.run(f"rm -f {shquote(archive)}", timeout=60)
        verified = bool(remote_hash) and all(_valid_hash(e.get("sha256")) for e in source.entries)
        return {"files": len(entries), "bytes": sum(e["bytes"] for e in entries),
                "verified": verified, "sha256": remote_hash, "entries": entries}

    def _remote_sha256(self, transport: SshTransport, archive: str) -> str | None:
        q = shquote(archive)
        result = transport.run(
            f"if command -v sha256sum >/dev/null 2>&1; then sha256sum < {q}; "
            f"elif command -v shasum >/dev/null 2>&1; then shasum -a 256 < {q}; "
            "else true; fi", timeout=120,
        )
        if result.returncode:
            return None
        parts = result.stdout.strip().split()
        candidate = parts[0] if parts else ""
        return candidate.lower() if _valid_hash(candidate) else None


# Helpers are shared by local copies and archive extraction; neither follows
# links nor overwrites files in-place (hard links included).

def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _fingerprints(entries: list[dict[str, Any]]) -> dict[str, tuple[Any, Any]]:
    return {path: (entry["size"], entry.get("sha256"))
            for path, entry in _manifest_paths(entries).items()}


def _listed_root(entries: list[dict[str, Any]], fallback: str) -> str:
    roots = {entry.get("source_root", fallback) for entry in entries}
    if len(roots) != 1:
        raise ExecutorError("output manifest contains inconsistent source roots")
    return str(roots.pop())


def _check_chain(path: Path) -> None:
    for part in (*reversed(path.parents), path):
        try:
            st = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            # macOS exposes /var, /tmp and /etc as OS-owned aliases into
            # /private. They are mount-point indirection, not a user-controlled
            # symlink component; rejecting them makes every TemporaryDirectory
            # under /var/folders unusable. Deeper components are still checked.
            if _is_darwin_system_alias(part):
                continue
            raise Conflict(f"refusing symlink/reparse path: {part}")


def _is_darwin_system_alias(part: Path) -> bool:
    if sys.platform != "darwin":
        return False
    text = str(part)
    if text not in {"/var", "/tmp", "/etc"}:
        return False
    try:
        return os.path.realpath(text).startswith("/private/")
    except OSError:
        return False


def _prepare_destination(dest: Path, *, force: bool) -> None:
    _check_chain(dest)
    if dest.exists() and not dest.is_dir():
        raise Conflict(f"destination {dest} exists and is not a directory")
    if dest.is_dir() and any(dest.iterdir()) and not force:
        raise Conflict(f"destination {dest} is not empty",
                       hint="choose an empty directory or pass --force to merge into it")
    dest.mkdir(parents=True, exist_ok=True)
    _check_chain(dest)


def _safe_relative(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise RcfError(f"refusing invalid manifest path: {path!r}")
    # ``find`` on some hosts emits ``./a/b``; that leading marker is not a
    # traversal. Normalize it before validating every component.
    while path.startswith("./"):
        path = path[2:]
    if not path:
        raise RcfError(f"refusing invalid manifest path: {path!r}")
    # Backslash is a genuine POSIX filename character, not a separator. Reject
    # it on Windows rather than silently copying to a different relative path.
    parts = path.split("/")
    if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise RcfError(f"refusing unsafe manifest path: {path!r}")
    if any(":" in p for p in parts):
        raise RcfError(f"refusing drive/stream-qualified path: {path!r}")
    if os.name == "nt" and any("\\" in p or p.endswith((".", " ")) or
                                p.split(".")[0].upper() in
                                {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                                 *[f"LPT{i}" for i in range(1, 10)]} for p in parts):
        raise RcfError(f"path cannot be represented safely on Windows: {path!r}")
    return path


def _manifest_paths(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    paths = {}
    aliases = set()
    for entry in entries:
        path = _safe_relative(entry["path"])
        alias = os.path.normcase(path)
        if path in paths or alias in aliases or not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise ExecutorError("duplicate/invalid manifest entry")
        paths[path] = entry
        aliases.add(alias)
    return paths


def _target_path(dest: Path, relative: str) -> Path:
    target = dest.joinpath(*_safe_relative(relative).split("/"))
    _check_chain(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_chain(target)
    if target.exists() and not target.is_file():
        raise Conflict(f"refusing non-file output target: {target}")
    return target


def _open_regular(path: Path):
    _check_chain(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ExecutorError(f"not a regular output file: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    stream = os.fdopen(fd, "rb")
    opened = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
        stream.close()
        raise ExecutorError(f"output file replaced while opening: {path}")
    return stream


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    _check_chain(path)
    last = path.lstat()
    # CRT fstat and lstat disagree about ctime semantics on Windows.
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                       s.st_ctime_ns if os.name != "nt" else None)
    if stamp(before) != stamp(after) or stamp(after) != stamp(last):
        raise ExecutorError(f"output file changed while hashing: {path}")
    return digest.hexdigest()


def _write_file(source, target: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=".rcf-copy-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            shutil.copyfileobj(source, stream, length=1024 * 1024)
        _check_chain(target)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_verified(archive: Path, dest: Path, expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths = _manifest_paths(expected)
    receipts = []
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        seen = set()
        for member in members:
            path = _safe_relative(member.name)
            if not member.isfile() or path in seen or path not in paths or member.size != paths[path]["size"]:
                raise ExecutorError(f"unexpected/unsafe archive member: {path!r}")
            seen.add(path)
        if seen != set(paths):
            raise ExecutorError("archive does not contain the complete output manifest")
        for member in members:
            path = member.name
            target = _target_path(dest, path)
            with tar.extractfile(member) as source:
                _write_file(source, target)
            sha = _sha256_file(target)
            expected_hash = paths[path].get("sha256")
            if target.lstat().st_size != member.size or (expected_hash and sha != expected_hash):
                raise ExecutorError(f"copied file checksum mismatch: {path!r}")
            receipts.append({"path": path, "bytes": member.size, "sha256": sha})
    return receipts
