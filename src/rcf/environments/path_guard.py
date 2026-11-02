"""Read-only, target-side evidence for managed environment prefix paths.

This complements (never replaces) ownership.check_prefix. Only ancestors are
inspected, not directory contents. Evidence expires immediately: another process
on a shared filesystem can replace an ancestor after inspection. Call again just
before mutation; this is not an atomic write primitive or a filesystem lock.
"""
from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass

from ..core.errors import RcfError
from ..core.models import NodeKind, NodeSpec
from ..core.runner import Runner
from ..executors.ssh import SshTransport
from .ownership import PrefixVerdict

# Standard-library-only Python 3.6+, also used locally to keep the same protocol.
# The script is constant; all user-controlled values arrive as JSON on stdin.
_INSPECT_SCRIPT = r'''
import json
import os
import stat
import sys


def inspect(request):
    out = dict(allowed=False, reason='', path='', exists=None, checked=[], missing_from=None)
    descriptors = []
    try:
        raw = request['path']
        if not isinstance(raw, str) or not raw or len(raw) > 32768 or '\x00' in raw:
            raise ValueError('prefix must be a nonempty path of at most 32768 characters')
        if '..' in raw.replace('\\', '/').split('/'):
            raise ValueError('parent traversal is not allowed')
        if raw == '~' or raw.startswith('~/') or (os.name == 'nt' and raw.startswith('~\\')):
            raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            raise ValueError('prefix must be absolute or start with ~/')
        if os.name == 'nt':
            drive, unused = os.path.splitdrive(raw)
            if (len(drive) != 2 or not drive[0].isalpha() or drive[1] != ':'
                    or ':' in raw[2:] or raw.startswith(('\\\\', '//'))):
                raise ValueError('device, UNC, root-relative and alternate-stream paths are blocked')
            # Win32 silently strips trailing dots/spaces and recognizes DOS device
            # names even with extensions. Do not attest a different filesystem name.
            reserved = set(['CON', 'PRN', 'AUX', 'NUL'] +
                           ['COM' + str(n) for n in range(1, 10)] +
                           ['LPT' + str(n) for n in range(1, 10)])
            for part in raw[2:].replace('\\', '/').split('/'):
                if part not in ('', '.') and (part.endswith((' ', '.')) or
                        part.split('.')[0].upper() in reserved):
                    raise ValueError('ambiguous or reserved Win32 path component')
        elif '\\' in raw:
            raise ValueError('backslash is not a POSIX path separator')
        path = os.path.normpath(raw)
        out['path'] = path
        drive, tail = os.path.splitdrive(path)
        current = drive + os.sep
        parts = [p for p in tail.split(os.sep) if p]
        if len(parts) > 256:
            raise ValueError('prefix exceeds 256 components')
        use_fd = os.name != 'nt'
        if use_fd and (not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
                       or os.stat not in os.supports_dir_fd or os.open not in os.supports_dir_fd
                       or os.stat not in os.supports_follow_symlinks):
            raise ValueError('safe ancestor inspection is unsupported on this platform')
        parent_fd = None
        for part in [None] + parts:
            if part is not None:
                current = os.path.join(current, part)
            try:
                if use_fd and parent_fd is not None:
                    info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                else:
                    info = os.lstat(current)
            except FileNotFoundError:
                # An absent root is an inspection failure, never a new prefix.
                if part is None:
                    raise ValueError('filesystem root cannot be inspected')
                out['missing_from'] = current
                out['exists'] = False
                break
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('symlink component blocked: ' + current)
            if os.name == 'nt':
                attrs = getattr(info, 'st_file_attributes', None)
                if attrs is None:
                    raise ValueError('cannot inspect reparse attributes: ' + current)
                if attrs & 0x400:
                    raise ValueError('reparse point component blocked: ' + current)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('prefix component is not a directory: ' + current)
            if use_fd:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = os.open(current, flags) if parent_fd is None else os.open(part, flags, dir_fd=parent_fd)
                descriptors.append(fd)
                pinned = os.fstat(fd)
                if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('prefix component changed during inspection: ' + current)
                parent_fd = fd
            out['checked'].append(dict(path=current, device=info.st_dev, inode=info.st_ino))
        else:
            out['exists'] = True
        # Windows has no dir_fd traversal. Recheck all observed ancestors without
        # following reparse points; still not a lock against external writers.
        if os.name == 'nt':
            for item in out['checked']:
                info = os.lstat(item['path'])
                attrs = getattr(info, 'st_file_attributes', None)
                if (attrs is None or attrs & 0x400 or not stat.S_ISDIR(info.st_mode)
                        or (info.st_dev, info.st_ino) != (item['device'], item['inode'])):
                    raise ValueError('prefix component changed during inspection: ' + item['path'])
        out['allowed'] = True
        out['reason'] = 'existing prefix components are directories without symlinks/reparse points'
    except (OSError, ValueError, TypeError, KeyError) as exc:
        out['allowed'] = False
        out['reason'] = 'cannot safely inspect prefix: ' + str(exc)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return out


print(json.dumps(inspect(json.load(sys.stdin)), ensure_ascii=True))
'''


@dataclass(frozen=True)
class PrefixPathInspection:
    """Snapshot only; ``allowed`` is path safety, not ownership authorization.

    ``exists=None`` means no complete existence evidence was obtained. The
    caller must not reinterpret it as a missing/new environment.
    """

    verdict: PrefixVerdict
    path: str = ""
    exists: bool | None = None
    checked_paths: tuple[str, ...] = ()
    missing_from: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict.allowed

    @property
    def blocked(self) -> bool:
        return self.verdict.blocked

    @property
    def reason(self) -> str:
        return self.verdict.reason


def inspect_prefix_path(node: NodeSpec, path: str, runner: Runner) -> PrefixPathInspection:
    """Inspect the prefix on its node, fail closed, never write or follow links.

    Run check_prefix first. After success, use the observed ``exists`` to repeat
    ownership checks if its earlier existence probe differed. Reinspect at apply
    time: the returned evidence cannot fence non-cooperating external writers.
    """

    def blocked(reason: str) -> PrefixPathInspection:
        return PrefixPathInspection(PrefixVerdict(False, reason))

    if not isinstance(path, str) or not path or len(path) > 32768 or "\x00" in path:
        return blocked("cannot safely inspect prefix: invalid or oversized path")
    payload = json.dumps({"path": path}, ensure_ascii=True)
    try:
        if node.kind is NodeKind.LOCAL:
            result = runner.run([sys.executable, "-c", _INSPECT_SCRIPT], stdin=payload, timeout=30)
        else:
            # The fixed script is shell-quoted; the path is solely stdin JSON.
            # JSON encodes embedded newlines, so data cannot end the heredoc.
            command = ("python3 -c " + shlex.quote(_INSPECT_SCRIPT)
                       + " <<'RCF_PREFIX_REQUEST'\n" + payload + "\nRCF_PREFIX_REQUEST\n")
            result = SshTransport.from_node(node, runner).run(command, timeout=30)
    except (RcfError, OSError, ValueError) as exc:
        return blocked("cannot safely inspect prefix: " + str(exc))
    if not result.ok:
        return blocked("prefix inspection failed or timed out: " + result.first_error_line())
    try:
        data = json.loads(result.stdout)
        if (not isinstance(data, dict) or type(data.get("allowed")) is not bool
                or not isinstance(data.get("reason"), str) or not data["reason"]
                or not isinstance(data.get("path"), str)
                or (data.get("exists") is not None and type(data["exists"]) is not bool)
                or not isinstance(data.get("checked"), list)
                or len(data["checked"]) > 257
                or not all(isinstance(item, dict) and isinstance(item.get("path"), str)
                           for item in data["checked"])
                or (data.get("missing_from") is not None
                    and not isinstance(data["missing_from"], str))):
            raise ValueError("invalid response schema")
        if data["allowed"] and (not data["checked"] or type(data["exists"]) is not bool
                                or not data["path"]
                                or (not data["exists"] and not data.get("missing_from"))):
            raise ValueError("incomplete inspection evidence")
        return PrefixPathInspection(
            verdict=PrefixVerdict(data["allowed"], data["reason"]), path=data["path"],
            exists=data["exists"], checked_paths=tuple(item["path"] for item in data["checked"]),
            missing_from=data.get("missing_from"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        return blocked("invalid prefix inspection response: " + str(exc))
