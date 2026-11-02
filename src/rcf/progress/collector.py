"""Bounded, read-only progress collection; log content is always data, never code."""
from __future__ import annotations

import base64
import json
import math
import posixpath
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import RcfError, SpecError
from ..core.ids import iso, now_utc
from ..core.models import NodeKind, NodeSpec
from ..core.runner import Runner
from ..executors.ssh import SshTransport

# Self-contained Python 3.6 helper. Paths and bounds arrive as JSON, not shell text.
# POSIX pins every ancestor with dir_fd/O_NOFOLLOW; Win32 pins non-reparse
# ancestor handles without delete/write sharing before opening the final file.
_READ_SCRIPT = r'''
import base64
import errno
import json
import os
import stat
import sys
import time

REQUEST = None


def win_open(path, directory):
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   wintypes.LPVOID, wintypes.DWORD]
    access = 0x80 if directory else 0x80000000
    handle = kernel.CreateFileW(path, access, 1 if directory else 3, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        code = ctypes.get_last_error()
        if code in (2, 3):
            raise OSError(errno.ENOENT, 'progress file or ancestor does not exist', path)
        if code in (5, 32):
            raise OSError(errno.EACCES, 'permission denied or unsafe sharing', path)
        raise OSError(code, 'safe progress handle open failed', path)
    attrs = (wintypes.DWORD * 2)()
    if not kernel.GetFileInformationByHandleEx(handle, 9, attrs, ctypes.sizeof(attrs)):
        kernel.CloseHandle(handle)
        raise ValueError('cannot inspect reparse attributes')
    if attrs[0] & 0x400 or bool(attrs[0] & 0x10) != directory:
        kernel.CloseHandle(handle)
        raise ValueError('link/reparse point or non-regular progress path blocked')
    if directory:
        return handle, kernel
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), None
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def read(request):
    out = dict(errors=[], complete=False, truncated=False, data='', source_path='', started_at='', finished_at='')
    descriptors = []
    handles = []
    try:
        root = os.path.expanduser(request['run_dir'])
        if not os.path.isabs(root) or '..' in root.replace('\\', '/').split('/'):
            raise ValueError('run_dir must be absolute (or ~/), without parent traversal')
        if os.name == 'nt' and (root.startswith(('\\\\', '//')) or ':' in root[2:]):
            raise ValueError('device, UNC and alternate-stream paths blocked')
        root = os.path.abspath(root)
        if os.path.basename(root) != request['run_id']:
            raise ValueError('run_dir must be the exact run_id directory, not a project path')
        name = 'progress.jsonl' if request['source'] == 'file' else 'run.log'
        out['source_path'] = os.path.join(root, name)
        drive, tail = os.path.splitdrive(root)
        base = drive + os.sep
        parts = [p for p in tail.split(os.sep) if p]
        if os.name == 'nt':
            current = base
            handle, kernel = win_open(current, True)
            handles.append((handle, kernel))
            for part in parts:
                current = os.path.join(current, part)
                handle, kernel = win_open(current, True)
                handles.append((handle, kernel))
            fd, unused = win_open(os.path.join(current, name), False)
            descriptors.append(fd)
            try:
                sfd, unused = win_open(os.path.join(current, 'started_at'), False)
                try:
                    raw_started = os.read(sfd, 64)
                finally:
                    os.close(sfd)
                started_text = raw_started.decode('ascii', 'ignore').strip()
                if started_text and len(started_text) <= 40:
                    out['started_at'] = started_text
            except (OSError, ValueError):
                pass
            try:
                ffd, unused = win_open(os.path.join(current, 'finished_at'), False)
                try:
                    raw_finished = os.read(ffd, 64)
                finally:
                    os.close(ffd)
                finished_text = raw_finished.decode('ascii', 'ignore').strip()
                if finished_text and len(finished_text) <= 40:
                    out['finished_at'] = finished_text
            except (OSError, ValueError):
                pass
        else:
            if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
                raise ValueError('safe no-follow descriptor access unavailable')

            def open_dir(directory, parent=None):
                # Traversing /public/home needs execute permission, not read.
                # Linux O_PATH pins an execute-only ancestor without opening it
                # for reading; older Python may not expose the constant.
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                try:
                    if parent is None:
                        return os.open(directory, flags)
                    return os.open(directory, flags, dir_fd=parent)
                except PermissionError:
                    path_only = getattr(os, 'O_PATH', 0)
                    if not path_only and sys.platform.startswith('linux'):
                        path_only = 0o10000000
                    if not path_only:
                        raise
                    flags = path_only | os.O_DIRECTORY | os.O_NOFOLLOW
                    if parent is None:
                        return os.open(directory, flags)
                    return os.open(directory, flags, dir_fd=parent)

            parent = open_dir(base)
            descriptors.append(parent)
            for part in parts:
                parent = open_dir(part, parent)
                descriptors.append(parent)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            descriptors.append(fd)
            # The executor records actual execution start here. Never use the
            # controller's submit time for runtime on a queued scheduler.
            try:
                sfd = os.open('started_at', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    raw_started = os.read(sfd, 64)
                finally:
                    os.close(sfd)
                started_text = raw_started.decode('ascii', 'ignore').strip()
                if started_text and len(started_text) <= 40:
                    out['started_at'] = started_text
            except OSError:
                pass
            try:
                ffd = os.open('finished_at', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    raw_finished = os.read(ffd, 64)
                finally:
                    os.close(ffd)
                finished_text = raw_finished.decode('ascii', 'ignore').strip()
                if finished_text and len(finished_text) <= 40:
                    out['finished_at'] = finished_text
            except OSError:
                pass
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('progress source must be a single regular file')
        if before.st_mtime > time.time() + 5:
            out['errors'].append('progress source mtime is in the future (node clock skew)')
        offset = max(0, before.st_size - request['max_bytes'])
        out['truncated'] = offset > 0
        # One look-behind byte distinguishes a line boundary from a split line.
        os.lseek(fd, max(0, offset - 1), os.SEEK_SET)
        remaining = before.st_size - max(0, offset - 1)
        chunks = []
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        partial = bool(raw) and not raw.endswith(b'\n')
        if remaining:
            out['errors'].append('progress source shrank during bounded read')
        if offset:
            preceding, raw = raw[:1], raw[1:]
            if preceding != b'\n':
                boundary = raw.find(b'\n')
                raw = raw[boundary + 1:] if boundary >= 0 else b''
        if partial:
            out['errors'].append('partial trailing progress line ignored')
            raw = raw[:raw.rfind(b'\n') + 1]
        after = os.fstat(fd)
        if after.st_size < before.st_size or after.st_mtime != before.st_mtime:
            out['errors'].append('progress source changed during bounded read')
        out['data'] = base64.b64encode(raw).decode('ascii')
        out['complete'] = not out['errors']
        if offset:
            out['errors'].append('byte limit: only a bounded tail was read; earlier history omitted')
    except (OSError, ValueError) as exc:
        if getattr(exc, 'errno', None) == errno.ENOENT:
            out['errors'].append('progress source or ancestor does not exist: ' + str(exc))
        elif getattr(exc, 'errno', None) == errno.EACCES:
            out['errors'].append('progress source permission denied: ' + str(exc))
        else:
            out['errors'].append('unsafe or unreadable progress source: ' + str(exc))
        out['complete'] = False
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
        for handle, kernel in reversed(handles):
            kernel.CloseHandle(handle)
    return out


request = json.loads(base64.b64decode(REQUEST).decode('utf-8')) if REQUEST else json.load(sys.stdin)
print(json.dumps(read(request), allow_nan=False))
'''

_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)$")
_FIELDS = {
    "run_id", "session_id", "seq", "timestamp", "elapsed_seconds", "phase", "completed",
    "total", "metric", "eta_reason", "finished",
}


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field: " + key)
        value[key] = item
    return value


def _non_json_constant(value: str) -> Any:
    raise ValueError("non-finite JSON constant: " + value)


def _validate(sample: Any, run_id: str) -> str | None:
    if not isinstance(sample, dict) or not _FIELDS <= sample.keys():
        return "missing required progress fields or non-object JSON"
    if not isinstance(sample["run_id"], str) or sample["run_id"] != run_id:
        return "run_id mismatch"
    if not isinstance(sample["session_id"], str) or not sample["session_id"].strip():
        return "invalid session_id"
    if type(sample["seq"]) is not int or sample["seq"] < 0:
        return "invalid seq: expected a non-negative integer"
    timestamp = sample["timestamp"]
    if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
        return "invalid timestamp: expected ISO UTC"
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "invalid timestamp: expected ISO UTC"
    for key in ("elapsed_seconds", "completed", "total", "metric"):
        value = sample[key]
        if key in ("total", "metric") and value is None:
            continue
        if not _finite(value) or (key != "metric" and value < 0):
            return "invalid finite/non-negative field: " + key
    if not isinstance(sample["phase"], str) or not sample["phase"].strip():
        return "invalid phase"
    if sample["eta_reason"] is not None and not isinstance(sample["eta_reason"], str):
        return "invalid eta_reason"
    if type(sample["finished"]) is not bool:
        return "invalid finished: expected bool (phase completion only)"
    return None


class Collector:
    """Collect from one exact run directory, never recursively or from project paths.

    ``complete`` describes the observed tail, not the entire log or run success.
    ``truncated`` reports byte/sample limits even if that tail is well-formed.
    """

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def read(
        self, node: NodeSpec, run_dir: str, run_id: str, *, source: str = "file",
        max_bytes: int = 262144, max_samples: int = 256, timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        if source not in {"file", "log_json"}:
            raise SpecError("progress source must be file or log_json")
        if (not isinstance(run_dir, str) or not run_dir or "\x00" in run_dir
                or not isinstance(run_id, str) or not run_id):
            raise SpecError("progress collection requires run_dir and run_id")
        if (type(max_bytes) is not int or not 1 <= max_bytes <= 16777216
                or type(max_samples) is not int or not 1 <= max_samples <= 65536
                or not _finite(timeout_seconds) or not 0 < timeout_seconds <= 3600):
            raise SpecError("invalid progress collection bounds")
        name = "progress.jsonl" if source == "file" else "run.log"
        path = str(Path(run_dir) / name) if node.kind is NodeKind.LOCAL else posixpath.join(run_dir, name)
        out: dict[str, Any] = {
            "samples": [], "errors": [], "complete": False, "truncated": False,
            "source_path": path, "observed_at": iso(now_utc()),
        }
        request = json.dumps({"run_dir": run_dir, "run_id": run_id, "source": source, "max_bytes": max_bytes})
        try:
            if node.kind is NodeKind.LOCAL:
                result = self.runner.run([sys.executable, "-c", _READ_SCRIPT], stdin=request,
                                         timeout=timeout_seconds)
            else:
                encoded = base64.b64encode(request.encode()).decode("ascii")
                script = _READ_SCRIPT.replace("REQUEST = None", "REQUEST = " + repr(encoded), 1)
                result = SshTransport.from_node(node, self.runner).run(
                    "python3 - <<'RCF_PROGRESS_READER'\n" + script + "\nRCF_PROGRESS_READER\n",
                    timeout=timeout_seconds,
                )
        except RcfError as exc:
            out["errors"].append(f"{exc.code}: {exc}")
            return out
        if not result.ok:
            out["errors"].append("progress helper timed out" if result.timed_out else
                                 "progress helper failed: " + result.first_error_line()[:600])
            return out
        try:
            reply = json.loads(result.stdout)
            if (not isinstance(reply, dict) or type(reply.get("complete")) is not bool
                    or type(reply.get("truncated")) is not bool
                    or not isinstance(reply.get("errors"), list)
                    or not all(isinstance(error, str) for error in reply["errors"])
                    or not isinstance(reply.get("data"), str)):
                raise ValueError("invalid response shape")
            if isinstance(reply.get("started_at"), str):
                out["started_at"] = reply["started_at"][:40]
            if isinstance(reply.get("finished_at"), str):
                out["finished_at"] = reply["finished_at"][:40]
            raw = base64.b64decode(reply["data"], validate=True)
            if len(raw) > max_bytes:
                raise ValueError("helper exceeded byte bound")
            text = raw.decode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            out["errors"].append("invalid progress helper response: " + str(exc))
            return out
        out.update(complete=reply["complete"], truncated=reply["truncated"])
        out["errors"].extend(reply["errors"])
        if isinstance(reply.get("source_path"), str) and reply["source_path"]:
            out["source_path"] = reply["source_path"]
        sessions: dict[str, dict[str, Any]] = {}
        samples: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if source == "log_json":
                if not line.startswith("RCF_PROGRESS "):
                    continue
                line = line[len("RCF_PROGRESS "):]
            try:
                sample = json.loads(line, object_pairs_hook=_json_object, parse_constant=_non_json_constant)
                problem = _validate(sample, run_id)
                if problem:
                    raise ValueError(problem)
                previous = sessions.get(sample["session_id"])
                if previous and (sample["seq"] <= previous["seq"]
                                 or sample["elapsed_seconds"] < previous["elapsed_seconds"]):
                    raise ValueError("invalid seq or non-monotonic session elapsed_seconds")
                if previous and sample["timestamp"]:
                    current_at = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
                    prior_at = datetime.fromisoformat(previous["timestamp"].replace("Z", "+00:00"))
                    if current_at < prior_at:
                        raise ValueError("non-monotonic session timestamp")
                sessions[sample["session_id"]] = sample
                samples.append(sample)
            except (ValueError, TypeError, OverflowError, RecursionError) as exc:
                out["errors"].append(f"progress line {line_number}: {exc}")
                out["complete"] = False
        if len(samples) > max_samples:
            out["truncated"] = True
            out["errors"].append("sample limit: earlier samples omitted from bounded tail")
        out["samples"] = samples[-max_samples:]
        out["observed_at"] = datetime.now(UTC).isoformat()
        return out
