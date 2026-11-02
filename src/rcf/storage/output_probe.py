"""Portable, fail-closed regular-file output enumeration (also sent over SSH).

The remote program deliberately uses Python's filesystem API rather than GNU
find's -printf, or newline-delimited filenames. No server-side RCF install is
needed; a Python 3 interpreter is required and absence is an explicit error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.errors import ExecutorError

# Keep this self-contained: it runs on the node without importing rcf.
PROBE = r'''
import hashlib
import json
import os
import stat
import tarfile


def linked(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def native_path(path):
    path = os.path.expanduser(path)
    # Git Bash fake-SSH may use /c/... with native Windows Python. Linux/WSL
    # interpreters leave their POSIX paths unchanged.
    if os.name == "nt" and len(path) > 3 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        path = path[1].upper() + ":" + path[2:]
    return os.path.abspath(path)


def checked(path):
    path = native_path(path)
    current = path
    original = os.lstat(path)
    while True:
        st = os.lstat(current)
        if linked(st):
            raise ValueError("symlink/reparse path refused: " + current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return original


def digest(path):
    before = checked(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("not a regular file: " + path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("file replaced during probe: " + path)
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
        after = os.fstat(stream.fileno())
    last = checked(path)
    # Windows lstat ctime is creation time while CRT fstat exposes change time.
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                       s.st_ctime_ns if os.name != "nt" else None)
    if stamp(before) != stamp(after) or stamp(after) != stamp(last):
        raise ValueError("file changed during probe: " + path)
    return h.hexdigest(), last


def enumerate_outputs(root, limit, reference=None, hashes=True):
    if limit < 1:
        raise ValueError("manifest limit must be positive")
    root = native_path(root)
    try:
        root_stat = checked(root)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("output root is not a directory")
    since = None
    if reference:
        try:
            since = checked(os.path.expanduser(reference)).st_mtime_ns
        except FileNotFoundError:
            return []
    entries = []
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        checked(directory)
        if reference:
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in sorted(dirs):
            checked(os.path.join(directory, name))
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(directory, name)
            st = checked(path)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("non-regular output refused: " + path)
            if since is not None and st.st_mtime_ns < since:
                continue
            if len(entries) >= limit:
                raise ValueError("output manifest exceeds limit %s; raise the fetch limit" % limit)
            sha = None
            if hashes:
                sha, st = digest(path)
            entries.append({"path": os.path.relpath(path, root).replace(os.sep, "/"),
                            "size": st.st_size, "modified_epoch": st.st_mtime,
                            "sha256": sha, "source_root": root})
    return sorted(entries, key=lambda entry: entry["path"])


def pack_outputs(root, entries, archive):
    root = native_path(root)
    checked(root)
    # Exclusive creation avoids overwriting a link or another fetch's archive.
    fd = os.open(native_path(archive), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for entry in entries:
            relative = entry["path"]
            if relative.startswith("/") or any(p in ("", ".", "..") for p in relative.split("/")):
                raise ValueError("unsafe manifest path")
            path = os.path.join(root, *relative.split("/"))
            sha, st = digest(path)
            if st.st_size != entry["size"] or sha != entry.get("sha256"):
                raise ValueError("source changed after enumeration: " + relative)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if (st.st_dev, st.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("source replaced while packing")
                info = tarfile.TarInfo(relative)
                info.size = st.st_size
                info.mode = 0o600
                tar.addfile(info, source)
            final_hash, final_stat = digest(path)
            if final_hash != sha or final_stat.st_size != st.st_size:
                raise ValueError("source changed while packing: " + relative)
    return []


def main(request):
    try:
        if request.get("archive"):
            entries = pack_outputs(request["root"], request["entries"], request["archive"])
        else:
            entries = enumerate_outputs(request["root"], request["limit"],
                                        request.get("reference"), request.get("hashes", True))
        print(json.dumps({"complete": True, "entries": entries}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"complete": False, "error": str(exc)}, ensure_ascii=True))
        raise SystemExit(1)
'''


def output_command(
    root: str, *, limit: int = 200, reference: str | None = None,
    archive: str | None = None, entries: list[dict[str, Any]] | None = None,
    hashes: bool = True,
) -> str:
    request = {"root": root, "limit": int(limit), "reference": reference, "hashes": bool(hashes)}
    if archive is not None:
        request.update({"archive": archive, "entries": entries or []})
    payload = json.dumps(request, ensure_ascii=True)
    return (
        "if command -v python3 >/dev/null 2>&1; then rcf_python=python3; "
        "elif command -v python >/dev/null 2>&1; then rcf_python=python; "
        "else echo 'RCF: Python 3 required for safe output enumeration' >&2; exit 127; fi\n"
        '"$rcf_python" - <<\'RCF_OUTPUT_PROBE\'\n' + PROBE
        + "\nmain(json.loads(" + repr(payload) + "))\nRCF_OUTPUT_PROBE\n"
    )


def parse_output_manifest(text: str, *, returncode: int = 0) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
        if returncode or payload.get("complete") is not True:
            raise ValueError(payload.get("error") or "output probe failed")
        entries = payload["entries"]
        if not isinstance(entries, list):
            raise ValueError("output entries are not a list")
        return entries
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ExecutorError(f"cannot enumerate complete outputs: {exc}") from exc


def local_output_manifest(
    root: Path, *, limit: int = 200, reference: Path | None = None, hashes: bool = True,
) -> list[dict[str, Any]]:
    # Execute the exact same read-only algorithm locally and remotely.
    namespace: dict[str, Any] = {}
    exec(PROBE, namespace)
    try:
        return namespace["enumerate_outputs"](
            str(root), int(limit), str(reference) if reference else None, bool(hashes),
        )
    except (OSError, ValueError) as exc:
        raise ExecutorError(f"cannot enumerate complete outputs: {exc}") from exc
