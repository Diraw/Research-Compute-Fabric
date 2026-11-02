"""Lexical identities and conservative reference checks for registered roots."""

from __future__ import annotations

import fnmatch
import ntpath
import posixpath
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..core.errors import Conflict
from ..core.ids import now_utc
from ..core.models import NodeKind, RunPlan, RunRecord

CONTROLLER_NODE = "controller"


def normalized(path: str) -> str:
    text = str(path).replace("\\", "/").rstrip("/")
    if len(text) > 1 and text[1] == ":":
        return ntpath.normpath(text).replace("\\", "/").casefold()
    return posixpath.normpath(text)


def within(path: str, root: str) -> bool:
    child, parent = normalized(path), normalized(root)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def overlaps(first: str, second: str) -> bool:
    return within(first, second) or within(second, first)


def join(root: str, relative: str) -> str:
    return root.rstrip("/\\") + "/" + relative


def local_root(path: str) -> str:
    # absolute(), not resolve(): symlink components must remain visible to FS guards.
    return str(Path(path).expanduser().absolute())


def expand_root(node, path: str, runner) -> str:
    if node.kind is NodeKind.LOCAL:
        return local_root(path)
    if path == "~" or path.startswith("~/"):
        from ..core.nodeops import NodeOps

        home = NodeOps(runner).home_dir(node)
        if not home:
            raise Conflict("cannot resolve remote home for guarded root")
        return home.rstrip("/") + path[1:]
    return path


def project_root(config, project, node, root_id: str, store) -> str:
    if root_id == "code":
        path = project.path_for(node.id)
    elif root_id == "data":
        path = project.data_path_for(node.id)
    elif root_id == "cache":
        path = str(Path(config.home) / ".tmp") if node.kind is NodeKind.LOCAL else "~/.rcf-tmp"
    elif root_id.startswith("run:"):
        record = store.get_run(root_id[4:])
        if record is None or record.project_id != project.id or record.node_id != node.id:
            raise Conflict("run root does not belong to this project/node")
        path = record.result_dir
    else:
        raise Conflict("unknown root_id", detail={"root_id": root_id})
    if not path:
        raise Conflict("project root is not configured", detail={"root_id": root_id, "node": node.id})
    return local_root(path) if node.kind is NodeKind.LOCAL else path


def all_runs(store) -> list[RunRecord]:
    records = store.list_runs(limit=20001)
    if len(records) > 20000:
        raise Conflict("reference index exceeds the safety limit; cleanup is blocked")
    return records


def live_plans(config, store) -> list[RunPlan]:
    ttl = config.settings.runs.plan_ttl_minutes
    rows = store._all("SELECT plan_json FROM plans ORDER BY created_at DESC LIMIT 20001")
    if len(rows) > 20000:
        raise Conflict("plan reference index exceeds the safety limit")
    result = []
    for row in rows:
        plan = RunPlan.model_validate_json(row["plan_json"])
        if store.get_run(plan.run_id) is not None:
            continue
        if ttl <= 0 or plan.created_at + timedelta(minutes=ttl) >= now_utc():
            result.append(plan)
    return result


def config_fingerprint(config) -> str:
    from ..core.ids import stable_hash

    return stable_hash({
        "nodes": {k: v.model_dump(mode="json") for k, v in config.nodes.items()},
        "projects": {k: v.model_dump(mode="json") for k, v in config.projects.items()},
        "environments": {k: v.model_dump(mode="json") for k, v in config.environments.items()},
        "settings": config.settings.model_dump(mode="json"),
    }, length=64)


def reference_fingerprint(config, store, ledger) -> str:
    from ..core.ids import stable_hash

    return stable_hash({
        "config": config_fingerprint(config), "revision": ledger.revision(),
        "runs": [r.model_dump(mode="json") for r in all_runs(store)],
        "plans": [p.model_dump(mode="json") for p in live_plans(config, store)],
    }, length=64)


def _input_match(path: str, pattern: str) -> bool:
    """Match checkout-relative globs, including zero-directory ``**``.

    This uses lexical paths only: enumerating declared inputs during cleanup
    would add races and let an absent/partial input scan weaken protection.
    """
    parts = tuple(path.replace("\\", "/").split("/"))
    glob = tuple(p for p in pattern.replace("\\", "/").split("/") if p != ".")

    @lru_cache(None)
    def matches(i: int, j: int) -> bool:
        if j == len(glob):
            return i == len(parts)
        if glob[j] == "**":
            return matches(i, j + 1) or (i < len(parts) and matches(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], glob[j]) and matches(i + 1, j + 1)

    return matches(0, 0)


def protection_reasons(config, store, ledger, project_id: str, node_id: str,
                       root: str, entry: dict[str, Any], *, run_id: str | None = None,
                       runner) -> list[str]:
    """No scientific-value guesses: absent ownership/positive evidence stays protected."""

    node = config.node(node_id)

    def absolute(path: str | None) -> str | None:
        return expand_root(node, path, runner) if path else None

    full = join(root, entry["path"])
    reasons = []
    parts = entry["path"].replace("\\", "/").split("/")
    if any(p in {".git", ".rcf", ".rcf-storage"} for p in parts):
        reasons.append("metadata is never cleanup-owned")
    if entry.get("kind") != "file":
        reasons.append("only regular files can be selected")
    project = config.project(project_id)
    code_root = absolute(project.path_for(node_id))
    if code_root and within(full, code_root):
        relative = normalized(full)[len(normalized(code_root).rstrip("/")) + 1:]
        if any(_input_match(relative, pattern.casefold() if node.kind is NodeKind.LOCAL
                            and len(normalized(full)) > 1 and normalized(full)[1] == ":" else pattern)
               for pattern in project.inputs):
            reasons.append("declared input of project: " + project_id)
    data_root = absolute(project.data_path_for(node_id))
    if data_root and within(full, data_root):
        reasons.append("original/input data root is protected")
    for env in config.environments.values():
        path = absolute(env.path_for(node_id))
        if path and overlaps(full, path):
            reasons.append("environment prefix is protected")
    for other in config.projects.values():
        if other.id == project_id:
            continue
        for raw in (other.path_for(node_id), other.data_path_for(node_id)):
            path = absolute(raw)
            if path and overlaps(root, path):
                reasons.append("root overlaps another project's files")
    pins = {p["run_id"] for p in ledger.pins() if p.get("pinned", True)}
    # The requested run/root is not ownership evidence. Both the persisted
    # owner and every protected run referencing this physical output count.
    owner_ids = {run_id, entry.get("owner_id")}
    owner_ids.update(e.get("owner_id") for e in ledger.owned(project_id, node_id, normalized(root))
                     if e["path"] == entry["path"])
    if pins.intersection(owner_ids):
        reasons.append("run is pinned")
    records = all_runs(store)
    recent = set()
    scopes = {(r.project_id, r.node_id) for r in records if r.project_id in config.projects}
    for project_key, node_key in scopes:
        retention = config.project(project_key).storage_keep_recent_runs
        if retention is None:
            retention = config.settings.storage.keep_recent_runs
        recent.update(r.run_id for r in [r for r in records if r.project_id == project_key
                                        and r.node_id == node_key and r.state.value == "succeeded"][:retention])
    if recent.intersection(owner_ids):
        reasons.append("recent successful run retention")
    for record in records:
        if ledger.physical_node(record.node_id) != ledger.physical_node(node_id):
            continue
        if record.result_dir and within(full, absolute(record.result_dir)):
            if record.run_id in pins:
                reasons.append("pinned run references this output: " + record.run_id)
            if record.run_id in recent:
                reasons.append("recent successful run references this output: " + record.run_id)
        if not record.state.terminal:
            for path in (record.workdir, record.result_dir, record.handle.workdir if record.handle else None):
                if path and overlaps(full, path):
                    reasons.append("active/unknown run references this path: " + record.run_id)
        # Preserve declared input paths of all retained history; no implicit expiry of scientific references.
        if record.inputs and record.project_id in config.projects:
            origin = config.project(record.project_id)
            for file in record.inputs.files:
                input_root = (absolute(origin.data_path_for(node_id))
                              if file.root.value == "data" else record.workdir)
                if input_root and normalized(full) == normalized(join(input_root, file.path)):
                    reasons.append("recorded input of run: " + record.run_id)
    for plan in live_plans(config, store):
        if ledger.physical_node(plan.node_id) == ledger.physical_node(node_id) and any(path and overlaps(full, path)
                                           for path in (plan.project_path, plan.result_dir)):
            reasons.append("unexpired plan references this path: " + plan.plan_id)
    return list(dict.fromkeys(reasons))
