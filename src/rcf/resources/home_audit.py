"""Read-only inventory of files outside the roots RCF can account for.

This is deliberately not a cleanup surface.  It answers one question: what is
sitting in a node's home directory that RCF's storage ledger, environment
ownership and project roots do not cover?  The walk is bounded by entry count
and wall time, never follows symlinks, and returns partial results rather than
guessing.

Registered roots and large installer trees are pruned instead of walked.  That
keeps the command useful on an NFS home with hundreds of thousands of files and
keeps the answer focused on the unmanaged part.  The same self-contained script
runs on the controller (``python -c``) and on a remote POSIX node (``python3 -``
with a heredoc).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..core.errors import RcfError
from ..core.ids import now_utc
from ..core.models import NodeKind, NodeSpec
from ..core.nodeops import NodeOps
from ..core.runner import Runner, shquote

#: Python 3.6-compatible on purpose: CentOS 7 login nodes still ship 3.6.
AUDIT_SCRIPT = r'''
import json
import os
import sys
import time

options = json.loads(sys.argv[1])
home_raw = os.path.abspath(os.path.expanduser(options["home"]))
home = os.path.normcase(os.path.normpath(home_raw))
managed_raw = [str(p) for p in options.get("managed", [])]
known_raw = [str(p) for p in options.get("known", [])]
max_entries = int(options.get("max_entries", 300000))
max_seconds = float(options.get("max_seconds", 120))
started = time.time()

def normalized(path):
    return os.path.normcase(os.path.normpath(path))

def under(path, root):
    if path == root:
        return True
    return path.startswith(root.rstrip(os.sep) + os.sep)

PRUNE_TOOLCHAINS = ("miniforge3", "miniconda3", "anaconda3")
toolchains = []
for name in PRUNE_TOOLCHAINS:
    candidate = os.path.join(home_raw, name)
    if os.path.isdir(candidate) and not os.path.islink(candidate):
        toolchains.append(candidate)

prune_raw = managed_raw + known_raw + toolchains
pruned = [normalized(p) for p in prune_raw]

def is_pruned(path):
    target = normalized(path)
    for root in pruned:
        if under(target, root):
            return True
    return False

container = set()
for root in prune_raw:
    if under(normalized(root), home) and normalized(root) != home:
        first = os.path.relpath(normalized(root), home).split(os.sep)[0]
        if first not in (".", ".."):
            container.add(first)

def tree_key(parts):
    if len(parts) > 1 and parts[0] in container:
        return parts[0] + "/" + parts[1]
    return parts[0]

CACHES = {".cache", ".npm", ".cupy", ".pytest_cache", ".codex", ".modelscope", ".triton", ".nv", "snap"}
TOOLCHAINS = {".venvs", ".local"}
TEMP_ROOTS = {".rcf-tmp", "rcf-tmp"}

scanned = [0, 0]
loose = [0, 0]
residue = [0, 0]
caches = {}
envs = {}
trees = {}
temp = [0, 0]
temp_items = []
stale_push = 0
user_site_files = 0
legacy_temp = False
errors = 0
seen = 0
truncated = None

stack = [home_raw]
while stack and truncated is None:
    directory = stack.pop()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        errors += 1
        continue
    for entry in entries:
        if time.time() - started > max_seconds:
            truncated = "seconds"
            break
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if not is_pruned(entry.path):
                    stack.append(entry.path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if is_pruned(entry.path):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            errors += 1
            continue
        seen += 1
        if seen > max_entries:
            truncated = "entries"
            break
        try:
            relative = os.path.relpath(entry.path, home_raw)
        except ValueError:
            relative = entry.path
        parts = relative.split(os.sep)
        size = int(info.st_size)
        top = parts[0]
        scanned[0] += 1
        scanned[1] += size
        if top in TEMP_ROOTS:
            temp[0] += 1
            temp[1] += size
            if top == "rcf-tmp":
                legacy_temp = True
            if len(parts) <= 2 and len(temp_items) < 50:
                temp_items.append({
                    "path": relative,
                    "bytes": size,
                    "age_hours": round((time.time() - info.st_mtime) / 3600.0, 1),
                })
            name = parts[-1]
            if name.startswith(".rcf-push-") and name.endswith(".tar.gz"):
                if time.time() - info.st_mtime > 86400:
                    stale_push += 1
            continue
        if len(parts) == 1 and not top.startswith("."):
            loose[0] += 1
            loose[1] += size
        handled = False
        if top in CACHES:
            key = top if len(parts) < 2 else top + "/" + parts[1]
            item = caches.setdefault(key, [0, 0])
            item[0] += 1
            item[1] += size
            handled = True
        elif top in TOOLCHAINS:
            if top == ".venvs" and len(parts) > 1:
                key = top + "/" + parts[1]
            else:
                key = top
            item = envs.setdefault(key, [0, 0])
            item[0] += 1
            item[1] += size
            handled = True
        elif "__pycache__" in parts or relative.endswith((".pyc", ".pyo")):
            residue[0] += 1
            residue[1] += size
        if top == ".local" and "site-packages" in parts and "lib" in parts:
            user_site_files += 1
        if len(parts) > 1 and not handled and not top.startswith("."):
            key = tree_key(parts)
            item = trees.setdefault(key, [0, 0])
            item[0] += 1
            item[1] += size

warnings = []
if legacy_temp:
    warnings.append("legacy ~/rcf-tmp exists; the configured RCF cache root is ~/.rcf-tmp")
if user_site_files:
    warnings.append("%d file(s) under ~/.local/lib/.../site-packages are outside RCF managed env roots" % user_site_files)
if stale_push:
    warnings.append("%d stale .rcf-push-*.tar.gz archive(s) older than 24h under ~/.rcf-tmp" % stale_push)
unregistered_venvs = sorted(k for k in envs if k.startswith(".venvs/"))
for name in unregistered_venvs[:20]:
    warnings.append(name + " is not registered in environments.toml")
if truncated:
    warnings.append("scan stopped early (%s); counts are partial" % truncated)
if errors:
    warnings.append("%d path(s) could not be read; counts are partial" % errors)

def rows(mapping):
    return [
        {"path": key, "files": value[0], "bytes": value[1]}
        for key, value in sorted(mapping.items(), key=lambda item: (-item[1][1], item[0]))
    ]

print(json.dumps({
    "home": home_raw,
    "skipped": {
        "managed": managed_raw,
        "known": known_raw,
        "toolchains": toolchains,
    },
    "scanned": {"files": scanned[0], "bytes": scanned[1]},
    "unmanaged": {"files": scanned[0] - temp[0], "bytes": scanned[1] - temp[1]},
    "rcf_temp": {
        "files": temp[0],
        "bytes": temp[1],
        "stale_push_archives": stale_push,
        "items": temp_items,
    },
    "loose_root_files": {"files": loose[0], "bytes": loose[1]},
    "caches": rows(caches),
    "unmanaged_envs": rows(envs),
    "python_build_residue": {"files": residue[0], "bytes": residue[1]},
    "unmanaged_trees": rows(trees)[:50],
    "warnings": warnings,
    "complete": truncated is None,
    "truncated": truncated,
    "errors": errors,
    "elapsed_s": round(time.time() - started, 2),
}, ensure_ascii=True))
'''


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _expand(path: str, home: str) -> str:
    text = str(path).strip()
    if text == "~":
        return home
    if text.startswith("~/"):
        return home.rstrip("/\\") + text[1:]
    return text


def _within(path: str, root: str) -> bool:
    child, parent = path.replace("\\", "/").rstrip("/"), root.replace("\\", "/").rstrip("/")
    return child == parent or child.startswith(parent + "/")


def _dedupe(paths: list[str]) -> list[str]:
    result: list[str] = []
    for path in sorted({p for p in paths if p}, key=len):
        if not any(_within(path, kept) for kept in result):
            result.append(path)
    return result


class HomeAuditor:
    """Answer what sits outside RCF's known roots; never delete or adopt."""

    def __init__(self, config, runner: Runner, *, node_ops: NodeOps | None = None) -> None:
        self.config = config
        self.runner = runner
        self.ops = node_ops or NodeOps(runner)

    def roots(self, node: NodeSpec, home: str) -> tuple[list[str], list[str]]:
        managed: list[str] = []
        if node.work_root:
            managed.append(_expand(node.work_root, home))
        for root in getattr(self.config, "managed_roots", []):
            managed.append(_expand(root, home))
        managed = _dedupe(managed)

        known: list[str] = []
        for project in getattr(self.config, "projects", {}).values():
            for path in (project.path_for(node.id), project.data_path_for(node.id)):
                if path:
                    known.append(_expand(path, home))
        for environment in getattr(self.config, "environments", {}).values():
            path = environment.path_for(node.id)
            if path:
                known.append(_expand(path, home))
        known = [p for p in _dedupe(known) if not any(_within(p, root) for root in managed)]
        return managed, known

    def audit(
        self,
        node: NodeSpec,
        *,
        max_entries: int = 300_000,
        max_seconds: float = 120.0,
    ) -> dict[str, Any]:
        home = self.ops.home_dir(node)
        if not home:
            raise RcfError(f"node {node.id!r} did not report $HOME")
        managed, known = self.roots(node, home)
        payload = {
            "home": home,
            "managed": managed,
            "known": known,
            "max_entries": max(1, int(max_entries)),
            "max_seconds": max(5.0, float(max_seconds)),
        }
        timeout = payload["max_seconds"] + 30
        if node.kind is NodeKind.LOCAL:
            result = self.runner.run(
                [sys.executable, "-c", AUDIT_SCRIPT, json.dumps(payload)],
                timeout=timeout,
            )
        else:
            command = (
                "python3 - " + shquote(json.dumps(payload))
                + " <<'RCF_HOME_AUDIT_SCRIPT'\n" + AUDIT_SCRIPT
                + "\nRCF_HOME_AUDIT_SCRIPT"
            )
            result = self.ops._shell(node, command, timeout=timeout)
        if not result.ok:
            raise RcfError(
                f"home audit failed on {node.id!r}: {result.first_error_line()}",
                detail={"node": node.id, "returncode": result.returncode},
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RcfError(
                f"home audit returned invalid JSON on {node.id!r}",
                detail={"error": str(exc)},
            ) from exc
        data.update(
            node_id=node.id,
            node_kind=node.kind.value,
            observed_at=now_utc().isoformat(),
        )
        return data
