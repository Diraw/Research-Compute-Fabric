"""RCF-owned install caches: where they live and how they are pruned.

The cache root is deliberately outside every project and environment prefix:
``~/.rcf-tmp/cache`` on a remote node and ``<RCF_HOME>/cache`` on the controller.
Only files under that exact root can be selected.  The scan never follows
symlinks, and every deletion is checked against the size/mtime observed during
the scan so a file rewritten in the meantime is skipped rather than removed.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any

from ..core.errors import RcfError, SpecError
from ..core.models import NodeKind, NodeSpec
from ..core.nodeops import NodeOps
from ..core.runner import Runner, shquote

#: Python 3.6-compatible: the same script runs on old CentOS login nodes.
CACHE_PRUNE_SCRIPT = r'''
import json
import os
import sys
import time

options = json.loads(sys.argv[1])
root = os.path.abspath(os.path.expanduser(str(options["root"])))
apply = bool(options.get("apply"))
older_than = float(options.get("older_than_seconds", 0))
max_bytes = int(options.get("max_bytes", 0))
started = time.time()

if os.path.islink(root):
    print(json.dumps({
        "root": root,
        "error": "cache root is a symlink; refusing to follow it",
        "files": 0, "bytes": 0, "selected_files": 0, "selected_bytes": 0,
        "deleted_files": 0, "deleted_bytes": 0, "changed_files": 0,
        "errors": 0, "apply": apply,
    }))
    raise SystemExit(0)

if not os.path.exists(root):
    print(json.dumps({
        "root": root, "missing": True,
        "files": 0, "bytes": 0, "selected_files": 0, "selected_bytes": 0,
        "deleted_files": 0, "deleted_bytes": 0, "changed_files": 0,
        "errors": 0, "apply": apply, "elapsed_s": 0.0,
    }))
    raise SystemExit(0)

files = []
total = 0
errors = 0
stack = [root]
while stack:
    directory = stack.pop()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        errors += 1
        continue
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            errors += 1
            continue
        files.append((entry.path, int(info.st_size), float(info.st_mtime)))
        total += int(info.st_size)

cutoff = started - older_than if older_than > 0 else None
selected = []
selected_paths = set()
for path, size, mtime in files:
    if cutoff is not None and mtime < cutoff:
        selected.append((path, size, mtime))
        selected_paths.add(path)

selected_bytes = sum(size for _, size, _ in selected)
if max_bytes > 0 and total - selected_bytes > max_bytes:
    remaining = sorted(
        (item for item in files if item[0] not in selected_paths),
        key=lambda item: item[2],
    )
    for path, size, mtime in remaining:
        if total - selected_bytes <= max_bytes:
            break
        selected.append((path, size, mtime))
        selected_bytes += size

deleted = 0
freed = 0
changed = 0
if apply:
    for path, size, mtime in selected:
        try:
            info = os.stat(path, follow_symlinks=False)
            if int(info.st_size) != size or abs(float(info.st_mtime) - mtime) > 1e-6:
                changed += 1
                continue
            os.unlink(path)
            deleted += 1
            freed += size
        except OSError:
            errors += 1

print(json.dumps({
    "root": root,
    "files": len(files),
    "bytes": total,
    "selected_files": len(selected),
    "selected_bytes": selected_bytes,
    "deleted_files": deleted,
    "deleted_bytes": freed,
    "changed_files": changed,
    "errors": errors,
    "apply": apply,
    "older_than_seconds": older_than,
    "max_bytes": max_bytes,
    "elapsed_s": round(time.time() - started, 2),
}, ensure_ascii=True))
'''

_SIZE_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024, "ki": 1024, "kib": 1024,
    "m": 1024**2, "mb": 1024**2, "mi": 1024**2, "mib": 1024**2,
    "g": 1024**3, "gb": 1024**3, "gi": 1024**3, "gib": 1024**3,
    "t": 1024**4, "tb": 1024**4, "ti": 1024**4, "tib": 1024**4,
}
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_size(value: str) -> int:
    """Parse ``5G`` / ``500M`` / ``0``; reject ambiguous or negative input."""

    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text)
    if match is None or match.group(2) not in _SIZE_UNITS:
        raise SpecError(
            f"cannot read {value!r} as a size",
            hint="use bytes or a unit such as 500M, 5G, 1TiB",
        )
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def parse_duration(value: str) -> int:
    """Parse ``90m`` / ``24h`` / ``30d``; ``0`` means no age cutoff."""

    text = str(value).strip().lower()
    if text == "0":
        return 0
    match = re.fullmatch(r"(\d+)\s*([smhdw])", text)
    if match is None:
        raise SpecError(
            f"cannot read {value!r} as a duration",
            hint="use a number and a unit: 90m, 24h, 30d",
        )
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


class CacheManager:
    """Select and optionally delete only files under RCF's own cache root."""

    def __init__(self, config, runner: Runner, *, node_ops: NodeOps | None = None) -> None:
        self.config = config
        self.runner = runner
        self.ops = node_ops or NodeOps(runner)

    def root(self, node: NodeSpec) -> str:
        if node.kind is NodeKind.LOCAL:
            return str(Path(self.config.home) / "cache")
        home = self.ops.home_dir(node)
        if not home:
            raise RcfError(f"node {node.id!r} did not report $HOME")
        return posixpath.join(home.rstrip("/"), ".rcf-tmp", "cache")

    def prune(
        self,
        node: NodeSpec,
        *,
        older_than_seconds: int = 30 * 86400,
        max_bytes: int = 5 * 1024**3,
        apply: bool = False,
    ) -> dict[str, Any]:
        root = self.root(node)
        payload = {
            "root": root,
            "older_than_seconds": max(0, int(older_than_seconds)),
            "max_bytes": max(0, int(max_bytes)),
            "apply": bool(apply),
        }
        if node.kind is NodeKind.LOCAL:
            result = self.runner.run(
                [sys.executable, "-c", CACHE_PRUNE_SCRIPT, json.dumps(payload)],
                timeout=300,
            )
        else:
            command = (
                "python3 - " + shquote(json.dumps(payload))
                + " <<'RCF_CACHE_PRUNE'\n" + CACHE_PRUNE_SCRIPT
                + "\nRCF_CACHE_PRUNE"
            )
            result = self.ops._shell(node, command, timeout=300)
        if not result.ok:
            raise RcfError(
                f"cache prune failed on {node.id!r}: {result.first_error_line()}",
                detail={"node": node.id, "returncode": result.returncode},
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RcfError(
                f"cache prune returned invalid JSON on {node.id!r}",
                detail={"error": str(exc)},
            ) from exc
        if data.get("error"):
            raise RcfError(
                f"cache prune refused on {node.id!r}: {data['error']}",
                detail={"node": node.id, "root": root},
            )
        data.update(node_id=node.id, node_kind=node.kind.value)
        return data
