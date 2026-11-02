"""Copy a project checkout to another node, without clobbering work done there.

`project sync` is the git way (clone/pull).  This is the other half: a project
that is *not* driven by a remote, or a quick fix that is not committed yet, still
has to reach the machine that runs it.

Rules that make this safe enough to run against a real checkout:

* It is a two-phase operation: a plan that names every file, then an apply.
* Only files that differ are sent; unchanged ones are left alone.
* A file on the target that is newer than the source is **not** overwritten
  unless ``force`` is set — that is somebody's edit, not stale data.
* Nothing is ever deleted on the target.
* Transferred files are hashed on both ends after extraction.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ..core.errors import NotFound, RcfError, UnsupportedOperation
from ..core.ids import new_id
from ..core.models import (
    Check,
    NodeKind,
    NodeSpec,
    ProbeStatus,
    ProjectSpec,
    PushPlan,
    PushReport,
    SyncFile,
)
from ..core.nodeops import NodeOps
from ..core.runner import Runner, shquote
from ..executors.ssh import SshTransport
from ..security.redaction import redact

#: Files that are never worth copying.  A project can add its own via
#: ``sync_exclude`` in ``projects.toml``.
DEFAULT_EXCLUDES = [
    # .git travels with the checkout (commit provenance has to reach the node),
    # except for its working-tree cache, which is per-machine and would only
    # ever show up as a spurious "newer on the target".
    ".git/index",
    ".git/logs",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    "node_modules",
    ".venv",
    "venv",
    "outputs",
    "*.log",
]

#: Clock tolerance used only to decide "the target copy is newer": a file edited
#: on the node an hour ago must not be overwritten by a checkout that merely has
#: a slightly ahead clock.
MTIME_TOLERANCE_S = 300.0

#: Refuse to pack more than this in one go: the archive lands in the target's
#: run root and a runaway selection should not fill a quota silently.
MAX_PUSH_BYTES = 2 * 1024**3
HASH_BATCH_SIZE = 128


def _is_excluded(relative: str, patterns: list[str]) -> bool:
    parts = relative.split("/")
    for raw in patterns:
        pattern = raw.strip("/")
        if not pattern:
            continue
        if fnmatch.fnmatchcase(relative, pattern):
            return True
        for depth in range(1, len(parts)):
            if fnmatch.fnmatchcase("/".join(parts[:depth]), pattern):
                return True
    return False


def list_files_command() -> str:
    """A file listing that works on both GNU and BSD userlands.

    ``find -printf`` is a GNU extension that macOS's BSD find rejects, and BSD
    ``stat`` uses ``-f`` with a printf-style format that does *not* expand
    backslash escapes.  So the two branches are spelled differently on purpose:
    GNU find is given the escapes it understands, BSD stat is given real tabs.
    """

    gnu = "find . -type f -printf '%s\t%T@\t%p\n'"
    bsd = "find . -type f -exec stat -f '%z" + "\t" + "%m" + "\t" + "%N' {} +"
    return (
        "if find . -maxdepth 0 -printf '' >/dev/null 2>&1; then "
        f"{gnu}; else {bsd}; fi"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_hashes(paths: list[str], hashes: dict[str, str], context: str) -> None:
    if set(hashes) != set(paths) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in hashes.values()
    ):
        raise RcfError(
            f"incomplete or invalid hashes for {context}",
            hint="every requested file must have a valid SHA-256; retry after checking file access",
            detail={"missing": sorted(set(paths) - set(hashes))[:10],
                    "unexpected": sorted(set(hashes) - set(paths))[:10]},
        )


def _relative_path(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if (not path.parts or path.is_absolute() or ".." in path.parts
            or "\\" in relative or ":" in relative or "\n" in relative or "\r" in relative):
        raise RcfError(f"unsafe push file path: {relative!r}")
    return path


def _local_info(path: Path) -> os.stat_result:
    """Reject links (including Windows junctions) in every existing component."""
    absolute = path.absolute()
    try:
        for component in [*reversed(absolute.parents), absolute]:
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) or 0) & 0x400:
                raise RcfError(f"push refuses symbolic links or reparse points: {component}")
        return info
    except OSError as exc:
        raise RcfError(f"cannot inspect push path {path}: {exc}") from exc


def _open_local_file(root: Path, relative: str) -> BinaryIO:
    full = root / _relative_path(relative)
    info = _local_info(full)
    if not stat.S_ISREG(info.st_mode):
        raise RcfError(f"push requires a regular file, not a special file: {full}")
    try:
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        handle = os.fdopen(fd, "rb")
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            handle.close()
            raise RcfError(f"push file changed while opening: {full}")
        return handle
    except OSError as exc:
        raise RcfError(f"cannot open push file {full}: {exc}") from exc


# Sent unchanged to the SSH target. Never open an existing inode for writing.
_SAFE_EXTRACT = r'''
import os
import shutil
import stat
import tarfile
import uuid


def native(path):
    path = os.path.expanduser(path)
    if os.name == 'nt' and len(path) > 2 and path[0] == '/' and path[2] == '/' and path[1].isalpha():
        path = path[1].upper() + ':' + path[2:]
    return os.path.abspath(path)


def check(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('push refuses symbolic links or reparse points: ' + path)
    return info


def pin_windows_directory(path):
    # Deny WRITE and DELETE sharing so an ancestor cannot become a junction
    # in-place or be renamed. LIST_DIRECTORY makes Windows enforce sharing;
    # READ_ATTRIBUTES alone does not. Child create/unlink stays available;
    # Windows replacement is explicitly non-atomic under this strict pin.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(path, 0x81, 0x1, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), 'cannot pin push directory', path)
    try:
        if not stat.S_ISDIR(check(path).st_mode):
            raise ValueError('push parent is not a directory')
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def replace_windows_file(source, name, parent_fd):
    # Strict directory sharing blocks both reparse mutation and MoveFileEx.
    # Keep the verified parent pinned, break the old directory entry, and create
    # a fresh inode exclusively. Never relax the pin to obtain atomic rename.
    # A complete backup remains beside the staged file if restoration fails.
    import msvcrt
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                               wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(parent_fd)
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    destination = os.path.join(buffer.value, name)
    backup = source + '.previous'
    had_previous = os.path.lexists(destination)
    created = False
    if had_previous:
        if not stat.S_ISREG(check(destination).st_mode):
            raise ValueError('push refuses a linked or special destination')
        with open(destination, 'rb') as old, open(backup, 'xb') as saved:
            shutil.copyfileobj(old, saved)
            saved.flush()
            os.fsync(saved.fileno())
        os.unlink(destination)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        created = True
        with os.fdopen(fd, 'wb') as output, open(source, 'rb') as staged:
            shutil.copyfileobj(staged, output)
            output.flush()
            os.fsync(output.fileno())
        info = os.stat(source)
        os.chmod(destination, stat.S_IMODE(info.st_mode))
        os.utime(destination, (info.st_atime, info.st_mtime))
    except BaseException:
        if created:
            os.unlink(destination)
        if had_previous:
            # Exclusive creation cannot overwrite an unrelated racing entry.
            try:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
                with os.fdopen(fd, 'wb') as output, open(backup, 'rb') as saved:
                    shutil.copyfileobj(saved, output)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                raise RuntimeError('push failed; previous contents retained in ' + backup)
            os.unlink(backup)
        raise
    if had_previous:
        os.unlink(backup)


def directory(root, parts):
    path = root
    fds = []
    try:
        if os.name == 'nt':
            drive, tail = os.path.splitdrive(root)
            current = drive + os.sep
            fds.append(pin_windows_directory(current))
            for part in tail.replace('\\', '/').split('/'):
                if part:
                    current = os.path.join(current, part)
                    try:
                        os.mkdir(current)
                    except FileExistsError:
                        pass
                    fds.append(pin_windows_directory(current))
            for part in parts:
                path = os.path.join(path, part)
                try:
                    os.mkdir(path)
                except FileExistsError:
                    pass
                fds.append(pin_windows_directory(path))
            return path, fds
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/', flags)
        fds.append(fd)
        for part in root.split('/')[1:] + parts:
            if not part:
                continue
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            # Keep directory descriptors open through replacement. Existing
            # symlinks cannot redirect a regular tar member outside the root.
            try:
                fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise ValueError('push refuses linked or invalid directory: ' + part) from exc
            fds.append(fd)
        return fd, fds
    except BaseException:
        for fd in reversed(fds):
            os.close(fd)
        raise


def extract(root, archive, paths):
    root, archive = native(root), native(archive)
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        names = []
        for member in members:
            name = member.name[2:] if member.name.startswith('./') else member.name
            parts = name.split('/')
            if (not member.isfile() or not name or name.startswith('/')
                    or any(p in ('', '.', '..') for p in parts)
                    or any(c in name for c in ('\\', ':', '\x00'))):
                raise ValueError('unsafe push archive member: ' + name)
            names.append(name)
        if len(set(names)) != len(names) or set(names) != set(paths):
            raise ValueError('push archive differs from planned files')
        for member, name in zip(members, names):
            parent, fds = directory(root, name.split('/')[:-1])
            leaf = name.split('/')[-1]
            temporary = '.rcf-push-file-' + uuid.uuid4().hex
            try:
                if os.name == 'nt':
                    dest = os.path.join(parent, leaf)
                    if os.path.lexists(dest) and not stat.S_ISREG(check(dest).st_mode):
                        raise ValueError('push requires a regular destination file')
                    temp = os.path.join(parent, temporary)
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
                else:
                    try:
                        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    if info is not None and not stat.S_ISREG(info.st_mode):
                        raise ValueError('push refuses linked or special destination file')
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(fd, 'wb') as output, tar.extractfile(member) as source:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                if os.name == 'nt':
                    os.chmod(temp, member.mode & 0o777)
                    os.utime(temp, (member.mtime, member.mtime))
                    replace_windows_file(temp, leaf, fds[-1])
                else:
                    os.chmod(temporary, member.mode & 0o777, dir_fd=parent)
                    os.utime(temporary, (member.mtime, member.mtime), dir_fd=parent, follow_symlinks=False)
                    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    if os.name == 'nt':
                        os.unlink(os.path.join(parent, temporary))
                    else:
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                for fd in reversed(fds):
                    os.close(fd)
'''


class ProjectPusher:
    def __init__(self, config, runner: Runner, *, node_ops: NodeOps | None = None) -> None:
        self.config = config
        self.runner = runner
        self.ops = node_ops or NodeOps(runner)

    # -- scanning ----------------------------------------------------------- #
    def _scan_local(self, root: Path, excludes: list[str]) -> dict[str, tuple[int, float]]:
        # Inspect components before is_dir()/iteration: both can follow a root link.
        try:
            root.lstat()
        except FileNotFoundError as exc:
            raise NotFound(f"source directory {root} does not exist") from exc
        except OSError as exc:
            raise RcfError(f"cannot inspect push directory {root}: {exc}") from exc
        if not stat.S_ISDIR(_local_info(root).st_mode):
            raise RcfError(f"push root is not a directory: {root}")
        found: dict[str, tuple[int, float]] = {}
        pending = [root]
        try:
            while pending:
                directory = pending.pop()
                _local_info(directory)
                for path in directory.iterdir():
                    relative = path.relative_to(root).as_posix()
                    if _is_excluded(relative, excludes):
                        continue
                    _relative_path(relative)
                    info = _local_info(path)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        found[relative] = (info.st_size, info.st_mtime)
                    else:
                        raise RcfError(f"push refuses special files; regular files only: {path}")
        except OSError as exc:
            raise RcfError(f"cannot scan push directory {root}: {exc}") from exc
        return found

    def _scan_remote(
        self, node: NodeSpec, root: str, excludes: list[str]
    ) -> dict[str, tuple[int, float]]:
        transport = SshTransport.from_node(node, self.runner)
        exists = self.ops.dir_exists(node, root)
        if exists is not ProbeStatus.OK:
            raise NotFound(f"target directory {root} does not exist on {node.id!r}")
        listing = transport.run(f"cd {shquote(root)} && {list_files_command()}", timeout=300)
        if listing.returncode != 0:
            raise RcfError(
                f"cannot list files on {node.id!r}: {redact(listing.first_error_line())}",
                hint="the node needs find(1) and stat(1) (GNU or BSD)",
            )
        found: dict[str, tuple[int, float]] = {}
        for line in listing.stdout.splitlines():
            size, sep, rest = line.partition("\t")
            if not sep:
                continue
            mtime, sep2, raw = rest.partition("\t")
            if not sep2:
                continue
            relative = raw[2:] if raw.startswith("./") else raw
            if not relative or _is_excluded(relative, excludes):
                continue
            found[relative] = (int(size), float(mtime))
        return found

    # -- hashing ------------------------------------------------------------ #
    def _hash_local(self, root: Path, paths: list[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for relative in paths:
            with _open_local_file(root, relative) as handle:
                hashes[relative] = hashlib.file_digest(handle, "sha256").hexdigest()
        _validate_hashes(paths, hashes, str(root))
        return hashes

    def _hash_remote(self, node: NodeSpec, root: str, paths: list[str]) -> dict[str, str]:
        if not paths:
            return {}
        transport = SshTransport.from_node(node, self.runner)
        hashes: dict[str, str] = {}
        for start in range(0, len(paths), HASH_BATCH_SIZE):
            batch = paths[start:start + HASH_BATCH_SIZE]
            # ./ prevents option injection in both GNU sha256sum and BSD shasum.
            quoted = " ".join(shquote("./" + str(_relative_path(p))) for p in batch)
            command = (
                f"cd {shquote(root)} && "
                f"if command -v sha256sum >/dev/null 2>&1; then sha256sum -- {quoted}; "
                f"else shasum -a 256 {quoted}; fi"
            )
            result = transport.run(command, timeout=600)
            if not result.ok:
                raise RcfError(
                    f"cannot hash files on {node.id!r}: {redact(result.first_error_line())}"
                )
            received: dict[str, str] = {}
            for line in result.stdout.splitlines():
                match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
                if match is None:
                    raise RcfError(f"invalid hash output on {node.id!r}")
                digest, name = match.groups()
                relative = name[2:] if name.startswith("./") else name
                if relative in received:
                    raise RcfError(f"duplicate hash output for {relative!r} on {node.id!r}")
                received[relative] = digest
            _validate_hashes(batch, received, node.id)
            hashes.update(received)
        _validate_hashes(paths, hashes, node.id)
        return hashes

    # -- planning ----------------------------------------------------------- #
    def plan(
        self,
        project: ProjectSpec,
        target: NodeSpec,
        *,
        source: NodeSpec,
        force: bool = False,
        scope: str = "code",
    ) -> PushPlan:
        if scope == "data":
            source_root = project.data_path_for(source.id)
            target_root = project.data_path_for(target.id)
            field, hint = "data_paths", "add it to [projects.<id>.data_paths]"
            excludes = list(project.sync_exclude)
        else:
            source_root = project.path_for(source.id)
            target_root = project.path_for(target.id)
            field, hint = "paths", "add it to [projects.<id>.paths]"
            excludes = [*DEFAULT_EXCLUDES, *project.sync_exclude]
        if not source_root:
            raise NotFound(
                f"project {project.id!r} has no {scope} root for source node {source.id!r}",
                hint=hint,
                detail={"field": field, "node": source.id},
            )
        if not target_root:
            raise NotFound(
                f"project {project.id!r} has no {scope} root for node {target.id!r}",
                hint=hint,
                detail={"field": field, "node": target.id},
            )
        if source.id == target.id:
            raise UnsupportedOperation("source and target are the same node; nothing to push")

        checks: list[Check] = []

        if source.kind is NodeKind.LOCAL:
            source_files = self._scan_local(Path(source_root), excludes)
        else:
            source_files = self._scan_remote(source, source_root, excludes)
        if scope == "code" and project.sync_include:
            source_files = {p: evidence for p, evidence in source_files.items()
                            if _is_excluded(p, project.sync_include)}
        checks.append(
            Check(name="source", status=ProbeStatus.OK, detail=f"{source_root} ({len(source_files)} files)")
        )
        large = [p for p, (size, _) in source_files.items() if size >= 50 * 1024**2]
        if scope == "code" and large:
            checks.append(Check(
                name="large_code_files", status=ProbeStatus.WARN,
                detail=f"{len(large)} file(s) >=50 MiB in the code transfer; consider data_paths/sync_include",
                data={"paths": large[:20]},
            ))

        target_missing = self.ops.dir_exists(target, target_root) is not ProbeStatus.OK
        if target_missing:
            checks.append(
                Check(
                    name="target",
                    status=ProbeStatus.WARN,
                    detail=f"{target_root} does not exist yet; apply will create it",
                )
            )
            target_files: dict[str, tuple[int, float]] = {}
        else:
            target_files = (
                self._scan_local(Path(target_root), excludes)
                if target.kind is NodeKind.LOCAL
                else self._scan_remote(target, target_root, excludes)
            )
            checks.append(
                Check(
                    name="target",
                    status=ProbeStatus.OK,
                    detail=f"{target_root} ({len(target_files)} files)",
                )
            )

        new: list[SyncFile] = []
        changed: list[SyncFile] = []
        remote_newer: list[SyncFile] = []
        candidates: list[str] = []
        unchanged = 0
        for relative, (size, mtime) in sorted(source_files.items()):
            remote = target_files.get(relative)
            if remote is None:
                new.append(SyncFile(path=relative, size=size, mtime=mtime))
                continue
            remote_size, remote_mtime = remote
            if remote_mtime > mtime + MTIME_TOLERANCE_S and not force:
                remote_newer.append(SyncFile(path=relative, size=remote_size, mtime=remote_mtime))
                continue
            if remote_size != size:
                changed.append(SyncFile(path=relative, size=size, mtime=mtime))
            else:
                candidates.append(relative)

        # Equal size and timestamps are not content evidence: edits can preserve
        # both, and tar often restores only whole-second timestamps.
        if candidates:
            source_hashes = (
                self._hash_local(Path(source_root), candidates)
                if source.kind is NodeKind.LOCAL
                else self._hash_remote(source, source_root, candidates)
            )
            target_hashes = (
                self._hash_local(Path(target_root), candidates)
                if target.kind is NodeKind.LOCAL
                else self._hash_remote(target, target_root, candidates)
            )
            _validate_hashes(candidates, source_hashes, f"source {source.id}")
            _validate_hashes(candidates, target_hashes, f"target {target.id}")
            for relative in candidates:
                size, mtime = source_files[relative]
                entry = SyncFile(path=relative, size=size, mtime=mtime, sha256=source_hashes[relative])
                if source_hashes[relative] == target_hashes[relative]:
                    unchanged += 1
                else:
                    changed.append(entry)

        total = sum(f.size for f in new) + sum(f.size for f in changed)
        if total > MAX_PUSH_BYTES:
            checks.append(
                Check(
                    name="size",
                    status=ProbeStatus.FAIL,
                    detail=f"{total} bytes exceeds the {MAX_PUSH_BYTES} byte limit; narrow the sync_exclude list",
                )
            )
        if remote_newer:
            checks.append(
                Check(
                    name="remote_newer",
                    status=ProbeStatus.WARN,
                    detail=(
                        f"{len(remote_newer)} file(s) are newer on {target.id}; left untouched "
                        "(use --force to override)"
                    ),
                )
            )

        return PushPlan(
            project_id=project.id,
            scope=scope,
            source_node=source.id,
            target_node=target.id,
            source_root=source_root,
            target_root=target_root,
            new=new,
            changed=changed,
            remote_newer=remote_newer,
            unchanged=unchanged,
            bytes=total,
            excludes=excludes,
            force=force,
            checks=checks,
        )

    # -- applying ----------------------------------------------------------- #
    def apply(self, project: ProjectSpec, target: NodeSpec, plan: PushPlan, *, source: NodeSpec) -> PushReport:
        if any(c.status is ProbeStatus.FAIL for c in plan.checks):
            raise RcfError(
                "push plan has blocking issues",
                hint="; ".join(c.name for c in plan.checks if c.status is ProbeStatus.FAIL),
            )
        send = [f.path for f in [*plan.new, *plan.changed]]
        if not send:
            return PushReport(
                project_id=project.id,
                source_node=source.id,
                target_node=target.id,
                target_root=plan.target_root,
                files=0,
                bytes=0,
                unchanged=plan.unchanged,
                verified=True,
            )

        local_target = target.kind is NodeKind.LOCAL
        transport = None if local_target else SshTransport.from_node(target, self.runner)
        if local_target:
            self._check_local_destination(Path(plan.target_root), send)
            Path(plan.target_root).mkdir(parents=True, exist_ok=True)
        else:
            assert transport is not None
            # Stage outside the checkout; linked target roots must not redirect
            # even the temporary upload into an unrelated directory.
            remote_stage = self._remote_tmp(target)
            created = transport.run(f"mkdir -p {shquote(remote_stage)}", timeout=120)
            if created.returncode != 0:
                raise RcfError(
                    f"cannot create push staging on {target.id!r}: "
                    f"{redact(created.first_error_line())}"
                )

        archive_name = f".rcf-push-{new_id('push')}.tar.gz"
        stage = self.config.home / "tmp"
        stage.mkdir(parents=True, exist_ok=True)
        local_archive = stage / archive_name
        expected: dict[str, str] = {}
        archive_digest: str | None = None

        try:
            expected = self._pack_file(source, plan.source_root, send, local_archive)
            _validate_hashes(send, expected, "packed source files")
            archive_digest = _sha256_file(local_archive)
            if local_target:
                self._check_local_destination(Path(plan.target_root), send)
                namespace: dict = {}
                exec(_SAFE_EXTRACT, namespace)
                try:
                    namespace["extract"](plan.target_root, str(local_archive), send)
                except (OSError, ValueError, tarfile.TarError) as exc:
                    raise RcfError(f"safe push extraction failed: {exc}") from exc
            else:
                assert transport is not None
                remote_archive = posixpath.join(remote_stage, archive_name)
                transport.push_file(local_archive, remote_archive)
                extracted = transport.run(
                    "python3 - <<'RCF_PUSH_EXTRACT'\n" + _SAFE_EXTRACT
                    + "\nextract(" + repr(plan.target_root) + ", " + repr(remote_archive)
                    + ", " + repr(send) + ")\nRCF_PUSH_EXTRACT\n", timeout=1800
                )
                if extracted.returncode != 0:
                    raise RcfError(
                        f"extracting the archive failed on {target.id!r}: "
                        f"{redact(extracted.stderr.strip())[-1000:]}"
                    )
                transport.run(f"rm -f {shquote(remote_archive)}", timeout=60)
        finally:
            local_archive.unlink(missing_ok=True)

        received = (
            self._hash_local(Path(plan.target_root), send)
            if local_target
            else self._hash_remote(target, plan.target_root, send)
        )
        _validate_hashes(send, received, "received files")
        mismatched = [p for p in send if received[p] != expected[p]]
        if mismatched:
            raise RcfError(
                f"{len(mismatched)} file(s) did not survive the transfer intact",
                hint="retry; if it persists the link or the target disk is corrupting data",
                detail={"files": mismatched[:10]},
            )

        source_meta = {entry.path: entry for entry in [*plan.new, *plan.changed]}
        return PushReport(
            project_id=project.id,
            source_node=source.id,
            target_node=target.id,
            target_root=plan.target_root,
            files=len(send),
            bytes=plan.bytes,
            unchanged=plan.unchanged,
            archive_sha256=archive_digest,
            verified=True,
            entries=[SyncFile(path=p, size=source_meta[p].size, mtime=source_meta[p].mtime,
                              sha256=received[p]) for p in send],
        )

    # -- packing ------------------------------------------------------------ #
    def _check_local_destination(self, root: Path, paths: list[str]) -> None:
        for destination in [root, *(root / _relative_path(p) for p in paths)]:
            existing = destination
            try:
                while True:
                    try:
                        existing.lstat()
                        break
                    except FileNotFoundError:
                        existing = existing.parent
                info = _local_info(existing)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise RcfError(f"push destination is a special file: {existing}")
            except OSError as exc:
                raise RcfError(f"cannot inspect push destination {destination}: {exc}") from exc

    def _pack_file(
        self, source: NodeSpec, root: str, paths: list[str], archive: Path
    ) -> dict[str, str]:
        if source.kind is NodeKind.LOCAL:
            return self._pack_local(Path(root), paths, archive)
        return self._pack_remote(source, root, paths, archive)

    def _pack_local(self, root: Path, paths: list[str], archive: Path) -> dict[str, str]:
        expected: dict[str, str] = {}
        with tarfile.open(archive, "w:gz") as tar:
            for relative in paths:
                with _open_local_file(root, relative) as handle:
                    info = os.fstat(handle.fileno())
                    member = tarfile.TarInfo(relative)
                    member.size = info.st_size
                    member.mode = stat.S_IMODE(info.st_mode)
                    member.mtime = info.st_mtime
                    expected[relative] = hashlib.file_digest(handle, "sha256").hexdigest()
                    handle.seek(0)
                    tar.addfile(member, handle)
        _validate_hashes(paths, expected, "local archive")
        return expected

    def _pack_remote(self, source: NodeSpec, root: str, paths: list[str], archive: Path) -> dict[str, str]:
        transport_source = SshTransport.from_node(source, self.runner)
        staging = self._remote_tmp(source)
        created = transport_source.run(f"mkdir -p {shquote(staging)}", timeout=60)
        if created.returncode != 0:
            raise RcfError(
                f"cannot create a staging directory on {source.id!r}: "
                f"{redact(created.first_error_line())}"
            )
        remote_tmp = posixpath.join(staging, f".rcf-push-{new_id('src')}.tar.gz")
        packed = transport_source.run(
            f"cd {shquote(root)} && tar -czf {shquote(remote_tmp)} -- "
            + " ".join(shquote("./" + str(_relative_path(p))) for p in paths),
            timeout=1800,
        )
        if packed.returncode != 0:
            raise RcfError(
                f"cannot pack {root} on {source.id!r}: {redact(packed.first_error_line())}"
            )
        try:
            transport_source.fetch_file(remote_tmp, archive, timeout=1800)
        finally:
            transport_source.run(f"rm -f {shquote(remote_tmp)}", timeout=60)
        expected = self._hash_remote(source, root, paths)
        _validate_hashes(paths, expected, "remote archive source")
        # Remote tar can include links or changed files after planning. Never
        # forward such an archive to an extractor without inspecting its members.
        packed_hashes: dict[str, str] = {}
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                relative = member.name[2:] if member.name.startswith("./") else member.name
                _relative_path(relative)
                if not member.isfile() or relative not in expected or relative in packed_hashes:
                    raise RcfError(f"push archive contains a link, special or unexpected file: {member.name!r}")
                handle = tar.extractfile(member)
                assert handle is not None
                with handle:
                    packed_hashes[relative] = hashlib.file_digest(handle, "sha256").hexdigest()
        _validate_hashes(paths, packed_hashes, "remote packed files")
        if packed_hashes != expected:
            raise RcfError("remote packed file hashes differ from source; retry after files stop changing")
        return expected

    def _remote_tmp(self, node: NodeSpec) -> str:
        home = self.ops.home_dir(node)
        return posixpath.join(home or "/tmp", ".rcf-tmp")
