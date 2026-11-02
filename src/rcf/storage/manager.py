"""Project-scoped inventory, ownership and explicitly confirmed cleanup.

This service never infers scientific value from a timestamp. Unknown ownership,
references, scans or copy evidence are blockers rather than permission to delete.
"""

from __future__ import annotations

import fnmatch
from datetime import timedelta

from ..core.errors import Conflict, NotFound, PermissionDenied
from ..core.ids import iso, new_id, now_utc, parse_iso
from ..core.models import NodeSpec
from .execution import execution_lock
from .fs import StorageFS
from .ledger import Ledger
from .models import CleanupPlan
from .roots import (
    CONTROLLER_NODE,
    all_runs,
    expand_root,
    join,
    normalized,
    overlaps,
    project_root,
    protection_reasons,
    reference_fingerprint,
)

CODE_EXCLUDES = [
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules",
]


def _entries(result: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in result.get("entries", [])}


def _match(left: dict, right: dict) -> bool:
    return bool(left.get("sha256")) and all(
        str(left.get(key)) == str(right.get(key)) for key in ("path", "bytes", "sha256")
    )


class StorageManager:
    def __init__(self, config, store, runner, *, actor="api"):
        self.config, self.store, self.runner, self.actor = config, store, runner, actor
        self.policy = config.policy()
        self.ledger = Ledger(store, nodes=config.nodes)
        self.fs = StorageFS(runner)

    def roots(self, project_id: str, node_id: str) -> list[tuple[str, str]]:
        project, node = self.config.project(project_id), self.config.node(node_id)
        result = []
        for root_id, path in (("code", project.path_for(node_id)), ("data", project.data_path_for(node_id))):
            if path:
                result.append((root_id, project_root(self.config, project, node, root_id, self.store)))
        for run in all_runs(self.store):
            # Planned/active runs have no stable output inventory yet; their paths
            # are still protected by reference checks, but scanning them would
            # only produce a misleading "missing directory" snapshot.
            if (run.project_id == project_id and run.node_id == node_id
                    and run.result_dir and run.state.terminal):
                result.append(("run:" + run.run_id, run.result_dir))
        return result

    def scan(self, project_id: str, node_id: str) -> dict:
        self.policy.check("storage.scan")
        node = self.config.node(node_id)
        snapshots = []
        for root_id, root in self.roots(project_id, node_id):
            previous = self.ledger.latest_snapshot(project_id, node_id, root_id)
            excludes = (CODE_EXCLUDES if root_id == "code" else None)
            snapshot = self.fs.scan(node, root, max_entries=self.config.settings.storage.max_entries,
                                    max_seconds=self.config.settings.storage.max_scan_seconds,
                                    excludes=excludes)
            snapshot.update(project_id=project_id, node_id=node_id, root_id=root_id, scan_id=new_id("scan"))
            snapshot["growth_bytes"] = (
                snapshot["logical_bytes"] - previous["logical_bytes"]
                if previous and previous.get("complete") and snapshot.get("complete")
                and normalized(previous["root"]) == normalized(snapshot["root"]) else None
            )
            self.ledger.save_snapshot(snapshot)
            snapshots.append(snapshot)
            # Only an RCF-created, matching run marker grants output ownership.
            if root_id.startswith("run:") and snapshot.get("complete"):
                self._register_run_outputs(root_id[4:], snapshot)
        return {"project_id": project_id, "node_id": node_id, "snapshots": snapshots,
                "warnings": self.check(project_id, node_id)["warnings"]}

    def _register_run_outputs(self, run_id: str, snapshot: dict) -> None:
        record = self.store.get_run(run_id)
        if not record or not record.state.terminal or not record.handle or not record.handle.workdir:
            return
        # run.json is an explicit executor marker; directory naming alone is not ownership.
        node = self.config.node(record.node_id)
        try:
            if node.kind.value == "local":
                import json
                from pathlib import Path
                path = Path(record.handle.workdir) / "run.json"
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
                    return
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                import json

                from ..core.runner import shquote
                from ..executors.ssh import SshTransport
                command = f"test ! -L {shquote(join(record.handle.workdir, 'run.json'))} && cat {shquote(join(record.handle.workdir, 'run.json'))}"
                result = SshTransport.from_node(node, self.runner).run(command, timeout=10)
                if not result.ok or len(result.stdout) > 1024**2:
                    return
                payload = json.loads(result.stdout)
            if payload.get("run_id") != run_id:
                return
        except Exception:
            return
        files = [entry for entry in snapshot["entries"] if entry.get("kind") == "file"]
        root_key = normalized(snapshot["root"])
        known = {entry["path"] for entry in self.ledger.owned(record.project_id, record.node_id, root_key)}
        new_files = [entry for entry in files if entry["path"] not in known]
        if not new_files:
            return
        # A metadata-only directory listing is not enough to authorize a later
        # deletion: capture content fingerprints once, at first registration.
        # If hashing cannot complete, the files stay unmanaged (and thus safe).
        try:
            evidence = self.fs.inspect(
                node, snapshot["root"], [entry["path"] for entry in new_files], hashes=True,
            )
        except Exception:
            # Leave outputs unmanaged rather than granting deletion rights
            # without a verified fingerprint.
            return
        if not evidence.get("complete"):
            return
        actual = _entries(evidence)
        verified = [entry for entry in new_files
                    if entry["path"] in actual and actual[entry["path"]].get("sha256")]
        if verified:
            self.ledger.record_owned(record.project_id, record.node_id, root_key,
                                     [actual[entry["path"]] for entry in verified],
                                     kind="output", owner_id=run_id)

    def report(self, project_id: str, node_id: str | None = None, *, largest=20) -> dict:
        self.policy.check("storage.report")
        self.config.project(project_id)
        snapshots = self.ledger.snapshots(project_id=project_id, node_id=node_id)
        latest = {}
        for snapshot in snapshots:
            key = (snapshot["node_id"], snapshot["root_id"])
            if key not in latest or snapshot["observed_at"] > latest[key]["observed_at"]:
                latest[key] = snapshot
        owned = self.ledger.owned(project_id=project_id, node_id=node_id)
        ownership = {(entry["node_id"], normalized(entry["root"]), entry["path"]): entry for entry in owned}
        files = []
        seen = set()
        for snapshot in latest.values():
            for entry in snapshot.get("entries", []):
                if entry.get("kind") != "file":
                    continue
                identity = (snapshot["node_id"], normalized(join(snapshot["root"], entry["path"])))
                if identity in seen:
                    continue
                seen.add(identity)
                owner = ownership.get((snapshot["node_id"], normalized(snapshot["root"]), entry["path"]))
                files.append({**entry, "node_id": snapshot["node_id"], "root_id": snapshot["root_id"],
                              "root": snapshot["root"], "classification": owner["kind"] if owner else "unmanaged",
                              "owner_id": owner.get("owner_id") if owner else None})
        groups = {}
        for entry in files:
            group = groups.setdefault(entry["classification"], {"files": 0, "logical_bytes": 0})
            group["files"] += 1
            group["logical_bytes"] += entry["bytes"]
        return {"project_id": project_id, "node_id": node_id, "file_count": len(files),
                "logical_bytes": sum(f["bytes"] for f in files), "classification": groups,
                "roots": [{k: v for k, v in s.items() if k != "entries"} for s in latest.values()],
                "largest": sorted(files, key=lambda f: f["bytes"], reverse=True)[:max(0, min(largest, 200))],
                "pins": self.ledger.pins(project_id), "complete": bool(latest) and all(s.get("complete") for s in latest.values()),
                "note": "Cached measurements; logical size is not guaranteed physical reclaim. Unmanaged files are protected."}

    def check(self, project_id: str, node_id: str | None = None) -> dict:
        report = self.report(project_id, node_id, largest=0)
        project = self.config.project(project_id)
        warnings = []
        if not report["roots"]:
            warnings.append("no storage inventory; run storage scan for each explicit node")
        if not report["complete"]:
            warnings.append("inventory absent or incomplete; never use it to authorize cleanup")
        for root in report["roots"]:
            stamp = parse_iso(root["observed_at"])
            if stamp and stamp + timedelta(hours=self.config.settings.storage.stale_scan_hours) < now_utc():
                warnings.append("stale inventory: " + root["root_id"])
        if project.storage_budget_bytes and report["logical_bytes"] > project.storage_budget_bytes:
            warnings.append("project logical-byte budget exceeded")
        if project.storage_budget_files and report["file_count"] > project.storage_budget_files:
            warnings.append("project file-count budget exceeded")
        return {"project_id": project_id, "node_id": node_id, "warnings": warnings,
                "logical_bytes": report["logical_bytes"], "file_count": report["file_count"]}

    def pin(self, run_id: str, *, pinned=True, reason="", confirm=False) -> dict:
        self.policy.check("storage.pin")
        if not confirm or not reason.strip():
            raise PermissionDenied("pin changes require confirmation and a reason")
        record = self.store.get_run(run_id)
        if record is None or not record.project_id:
            raise NotFound("unknown project run")
        token = self.ledger.acquire_guard(record.node_id, [record.result_dir or record.workdir], operation="pin", owner_id=run_id)
        try:
            result = self.ledger.set_pin(record.project_id, run_id, pinned, reason)
        finally:
            self.ledger.release_guard(token)
        self._audit("storage.pin", run_id=run_id, detail=result)
        return result

    def _base_plan(self, project_id, node_id, root_id, *, action, kind) -> dict:
        project, node = self.config.project(project_id), self.config.node(node_id)
        root = expand_root(node, project_root(self.config, project, node, root_id, self.store), self.runner)
        snapshot = self.fs.scan(node, root, max_entries=self.config.settings.storage.max_entries,
                                max_seconds=self.config.settings.storage.max_scan_seconds,
                                excludes=CODE_EXCLUDES if root_id == "code" else None)
        if not snapshot.get("complete"):
            raise Conflict("cleanup/adoption requires a complete fresh inventory", detail={"errors": snapshot.get("errors")})
        if action == "cleanup" and root_id.startswith("run:"):
            # Make the declared run directory self-describing even when the user
            # goes straight to a plan without a prior full project scan.
            self._register_run_outputs(root_id[4:], snapshot)
        now = now_utc()
        plan = CleanupPlan(
            plan_id=new_id("cleanup" if action == "cleanup" else "adopt"), action=action,
            project_id=project_id, node_id=node_id, root_id=root_id, root=snapshot["root"],
            created_at=iso(now), expires_at=iso(now + timedelta(minutes=self.config.settings.storage.plan_ttl_minutes)),
            revision=self.ledger.revision(), kind=kind,
        ).model_dump(mode="json")
        plan["snapshot"] = {k: v for k, v in snapshot.items() if k != "entries"}
        return plan, snapshot

    def plan_adoption(
        self, project_id, node_id, *, root_id, paths, kind="output", run_id=None,
        source_node_id=None,
    ) -> dict:
        self.policy.check("storage.plan")
        if kind not in {"code", "input", "output", "disposable", "cache"} or not paths:
            raise Conflict("adoption needs explicit file paths and a known classification")
        plan, snapshot = self._base_plan(project_id, node_id, root_id, action="adopt", kind=kind)
        node = self.config.node(node_id)
        wanted = sorted(set(paths))
        available = _entries(snapshot)
        if any(p not in available or available[p].get("kind") != "file" for p in wanted):
            raise Conflict("adoption selects absent or non-regular files")
        evidence = self.fs.inspect(node, plan["root"], wanted, hashes=True)
        if not evidence.get("complete"):
            raise Conflict("cannot fingerprint all adoption files", detail={"errors": evidence.get("errors")})
        if kind == "code":
            # Legacy code may only be registered when it still matches the
            # declared source checkout exactly. That gives future stale-code
            # pruning a trustworthy source identity instead of guessing.
            if root_id != "code" or not source_node_id or source_node_id == node_id:
                raise Conflict("code adoption needs --root code and a distinct --from source node")
            source = self.config.node(source_node_id)
            source_root = expand_root(
                source, project_root(self.config, self.config.project(project_id), source, "code", self.store),
                self.runner,
            )
            source_evidence = self.fs.inspect(source, source_root, wanted, hashes=True)
            if not source_evidence.get("complete"):
                raise Conflict("cannot fingerprint the source checkout for code adoption",
                               detail={"errors": source_evidence.get("errors")})
            source_entries = _entries(source_evidence)
            target_entries = _entries(evidence)
            mismatched = [path for path in wanted
                          if path not in source_entries or path not in target_entries
                          or not _match(source_entries[path], target_entries[path])]
            if mismatched:
                raise Conflict(
                    "selected target files do not match the current source checkout; push or review before adoption",
                    detail={"paths": mismatched[:50]},
                )
            plan["source_node_id"] = source_node_id
            plan["source_root"] = source_root
        elif source_node_id:
            raise Conflict("source_node_id is only meaningful for code adoption")
        plan["entries"] = evidence["entries"]
        plan["run_id"] = run_id
        plan["owner_id"] = run_id or new_id("legacy")
        if run_id:
            record = self.store.get_run(run_id)
            if not record or record.project_id != project_id or record.node_id != node_id:
                raise Conflict("adoption run does not belong to this project/node")
        plan["logical_bytes"] = sum(e["bytes"] for e in evidence["entries"])
        plan["reference_fingerprint"] = reference_fingerprint(self.config, self.store, self.ledger)
        self.ledger.save_plan(plan)
        return plan

    def plan_cleanup(self, project_id, node_id, *, kind="disposable", root_id=None,
                     source_node_id=None, run_id=None) -> dict:
        self.policy.check("storage.plan")
        if kind not in {"stale-code", "disposable", "replica", "cache"}:
            raise Conflict("unknown cleanup kind")
        root_id = root_id or ("run:" + run_id if run_id else "code" if kind == "stale-code" else None)
        if not root_id:
            raise Conflict("select an explicit root_id or run_id")
        plan, snapshot = self._base_plan(project_id, node_id, root_id, action="cleanup", kind=kind)
        root = plan["root"]
        node = self.config.node(node_id)
        project = self.config.project(project_id)
        run_id = root_id[4:] if root_id.startswith("run:") else run_id
        plan["run_id"] = run_id
        owners = {e["path"]: e for e in self.ledger.owned(project_id, node_id, normalized(root))}
        plan["source_node_id"] = source_node_id
        source_listing = None
        source_root = None
        if kind == "stale-code":
            if root_id != "code" or not source_node_id or source_node_id == node_id:
                raise Conflict("stale-code needs root code and a distinct explicit source node")
            source = self.config.node(source_node_id)
            source_root = expand_root(source, project_root(self.config, project, source, "code", self.store), self.runner)
            source_listing = self.fs.scan(source, source_root,
                                          max_entries=self.config.settings.storage.max_entries,
                                          max_seconds=self.config.settings.storage.max_scan_seconds,
                                          excludes=CODE_EXCLUDES)
            if not source_listing.get("complete"):
                raise Conflict("cannot prove source paths are gone with an incomplete source scan")
            plan["source_root"] = source_listing["root"]
        candidates = []
        for entry in snapshot["entries"]:
            if entry.get("kind") != "file":
                continue
            path, owner = entry["path"], owners.get(entry["path"])
            if not owner:
                continue
            reasons = protection_reasons(self.config, self.store, self.ledger, project_id, node_id,
                                         root, {**entry, "owner_id": owner["owner_id"]}, run_id=run_id, runner=self.runner)
            if owner["kind"] == "input":
                reasons.append("registered input is protected")
            if kind == "stale-code":
                if owner["kind"] != "code" or owner.get("source_node") != source_node_id:
                    continue
                if normalized(owner.get("source_root") or "") != normalized(source_root):
                    reasons.append("deployment source root changed")
                if path in _entries(source_listing):
                    continue
            elif kind == "disposable":
                declared = run_id and any(fnmatch.fnmatchcase(path, p) for p in project.disposable_globs)
                if owner["kind"] != "disposable" and not declared:
                    continue
            elif kind == "cache":
                if owner["kind"] != "cache":
                    continue
            elif kind == "replica":
                if owner["kind"] != "output":
                    continue
            if reasons:
                plan["blocked"].append({"path": path, "reasons": reasons})
            else:
                candidates.append((entry, owner))
        evidence = self.fs.inspect(node, root, [e["path"] for e, _ in candidates], hashes=True) if candidates else {"complete": True, "entries": []}
        if not evidence.get("complete"):
            raise Conflict("candidate fingerprinting incomplete", detail={"errors": evidence.get("errors")})
        current = _entries(evidence)
        for entry, owner in candidates:
            actual = current[entry["path"]]
            if not owner.get("sha256"):
                plan["blocked"].append({"path": entry["path"], "reasons": ["ownership has no content fingerprint"]})
                continue
            if not _match(owner, actual):
                plan["blocked"].append({"path": entry["path"], "reasons": ["file changed since registration/deployment"]})
                continue
            actual = {**actual, "owner_id": owner["owner_id"], "owner_kind": owner["kind"],
                      "reason": kind, "run_id": run_id}
            if kind == "replica":
                copy = self._verified_copy(project_id, node_id, root, actual, run_id=run_id)
                if copy is None:
                    plan["blocked"].append({"path": actual["path"], "reasons": ["no live verified retained copy"]})
                    continue
                actual["retained_copy"] = copy
            plan["entries"].append(actual)
        plan["logical_bytes"] = sum(e["bytes"] for e in plan["entries"])
        plan["estimated_reclaim_bytes"] = sum(
            e["allocated_bytes"] for e in plan["entries"]
        ) if plan["entries"] and all(e.get("allocated_bytes") is not None and e.get("nlink", 1) == 1 for e in plan["entries"]) else None
        plan["reference_fingerprint"] = reference_fingerprint(self.config, self.store, self.ledger)
        plan["reasons"] = ["exact regular files only; metadata and protected inputs are retained",
                           "logical deletion is not guaranteed physical reclaim (hardlinks, open handles, snapshots)"]
        self.ledger.save_plan(plan)
        return plan

    def _verified_copy(self, project_id, node_id, root, entry, *, run_id=None):
        for receipt in self.ledger.copies(project_id=project_id, run_id=run_id):
            if not receipt.get("complete") or not receipt.get("verified"):
                continue
            if receipt.get("source_node") != node_id or normalized(receipt.get("source_root", "")) != normalized(root):
                continue
            matching = next((e for e in receipt.get("entries", []) if _match(e, entry)), None)
            if not matching:
                continue
            dest_node = receipt.get("dest_node") or CONTROLLER_NODE
            dest_root = receipt.get("dest_root")
            same_host = self.ledger.physical_node(dest_node) == self.ledger.physical_node(node_id)
            if not dest_root or (same_host and normalized(dest_root) == normalized(root)):
                continue
            node = NodeSpec(id=CONTROLLER_NODE, kind="local") if dest_node == CONTROLLER_NODE else self.config.node(dest_node)
            result = self.fs.inspect(node, dest_root, [entry["path"]], hashes=True)
            found = _entries(result).get(entry["path"])
            if result.get("complete") and found and _match(entry, found):
                # A hardlink to the same inode is one physical object, not a retained replica.
                if same_host and str(found.get("device")) == str(entry.get("device")) and str(found.get("inode")) == str(entry.get("inode")):
                    continue
                return {"node_id": dest_node, "root": result["root"], "entry": found}
        return None

    def show_plan(self, plan_id: str) -> dict:
        self.policy.check("storage.report")
        plan = self.ledger.get_plan(plan_id)
        if plan is None:
            raise NotFound("unknown storage plan", detail={"plan_id": plan_id})
        return {"plan": plan, "journal": self.ledger.journal_entries(plan_id),
                "guards": [g for g in self.ledger.guards() if g.get("owner_id") == plan_id],
                "note": "Read-only evidence. Heartbeat age does not authorize takeover."}

    def reconcile(self, plan_id: str, *, confirm=False) -> dict:
        """Fence old deletion, record current evidence, retire without deleting.

        Protection/config changes may block resume, but must not permanently
        lock a root. Reconciliation never reuses the stale selection to mutate.
        """
        self.policy.check("storage.apply")
        if not confirm:
            raise PermissionDenied("storage reconciliation always requires explicit confirmation")
        with execution_lock(self.config.home, plan_id) as owner:
            old = self.ledger.get_plan(plan_id)
            if old is None:
                raise NotFound("unknown storage plan")
            if old.get("status") in {"completed", "superseded"}:
                return self._apply_owned(plan_id, confirm=True, resume=False, owner=owner)
            prior = old.get("execution") or {}
            node = self.config.node(old["node_id"])
            if prior and prior.get("endpoint_fingerprint") != self._endpoint_fingerprint(node):
                raise Conflict("node endpoint identity changed; cannot reconcile the old target")
            owner["endpoint_fingerprint"] = self._endpoint_fingerprint(node)
            plan = self.ledger.claim_plan(
                plan_id, allow_resume=True, owner=owner, ownership_verified=True,
                previous_token=prior.get("token"),
            )
            token = owner["token"]
            operation = "storage.adopt" if plan["action"] == "adopt" else "storage.apply"
            guards = []
            try:
                node = self.config.node(plan["node_id"])
                root = plan["root"]  # Original scope, not a newly configured directory.
                if prior and prior.get("endpoint_fingerprint") != self._endpoint_fingerprint(node):
                    raise Conflict("node endpoint identity changed; cannot reconcile the old target")
                guards = self.ledger.acquire_guards(
                    node.id, [root], operation=operation, owner_id=plan_id, resume_owner=plan_id,
                )
                target = self.fs.prepare_delete(node, root, plan_id) if plan["action"] == "cleanup" and plan["entries"] else None
                observed = self.fs.inspect(node, root, [e["path"] for e in plan["entries"]], hashes=True)
                report = {"plan_id": plan_id, "status": "superseded", "no-delete": True,
                          "observations": observed, "target_operation": target,
                          "note": "Old target helper fenced; no files deleted. Review a fresh plan before further cleanup."}
                self.ledger.update_plan(plan_id, "superseded", report=report, owner_token=token,
                                         stage="reconciled_without_deletion", target_operation=target)
                for guard in self.ledger.guards():
                    if guard.get("owner_id") == plan_id and guard.get("operation") == operation:
                        self.ledger.release_guard(guard["guard_id"])
                self._audit("storage.apply", node_id=node.id, detail=report)
                return report
            except BaseException as exc:
                try:
                    self.ledger.update_plan(plan_id, "unknown", owner_token=token, stage="reconcile_blocked",
                                             report={"error": str(exc)[:400], "guards": guards})
                except Exception:
                    pass
                raise

    @staticmethod
    def _endpoint_fingerprint(node) -> str:
        from ..core.ids import stable_hash

        return stable_hash(node.model_dump(mode="json"), length=64)

    def apply(self, plan_id: str, *, confirm=False, resume=False) -> dict:
        with execution_lock(self.config.home, plan_id) as owner:
            return self._apply_owned(plan_id, confirm=confirm, resume=resume, owner=owner)

    def _apply_owned(self, plan_id: str, *, confirm, resume, owner) -> dict:
        current = self.ledger.get_plan(plan_id)
        if not current:
            raise NotFound("unknown storage plan", detail={"plan_id": plan_id})
        operation = "storage.adopt" if current["action"] == "adopt" else "storage.apply"
        self.policy.check(operation)
        # Permanent operations never inherit the ordinary execute opt-out.
        if not confirm:
            raise PermissionDenied("storage changes always require explicit confirmation of this plan")
        if current.get("status") in {"completed", "superseded"}:
            previous_owner = current.get("execution") or {}
            if previous_owner and (previous_owner.get("controller") != owner["controller"]
                                   or previous_owner.get("lock_scope") != owner["lock_scope"]):
                raise Conflict("completed plan guard cleanup belongs to an unverifiable controller")
            # Crash after terminal commit but before finally releasing guards:
            # completion is durable, and the OS lock proves no local applier lives.
            for guard in self.ledger.guards():
                if guard.get("owner_id") == plan_id and guard.get("operation") == operation:
                    self.ledger.release_guard(guard["guard_id"])
            return current.get("report", {"plan_id": plan_id, "status": "completed"})
        previous_owner = current.get("execution") or {}
        node = self.config.node(current["node_id"])
        endpoint = self._endpoint_fingerprint(node)
        if previous_owner and previous_owner.get("endpoint_fingerprint") != endpoint:
            raise Conflict("node endpoint identity changed; resume cannot address the old target")
        owner["endpoint_fingerprint"] = endpoint
        claimed = self.ledger.claim_plan(
            plan_id, allow_resume=resume, owner=owner, ownership_verified=True,
            previous_token=previous_owner.get("token"),
        )
        if not claimed.get("_claimed"):
            return claimed.get("report", {"plan_id": plan_id, "status": "completed"})
        plan = claimed
        previous = claimed.get("_previous_status", "planned")
        token = owner["token"]
        recovering = resume and previous in {"applying", "unknown", "partial"}
        guards: list[str] = []
        held_roots: set[tuple[str, str]] = set()
        dispatched = False
        keep_guards = recovering

        def save(status, report=None, *, stage=None, target_operation=None):
            self.ledger.update_plan(plan_id, status, report=report, owner_token=token,
                                    stage=stage, target_operation=target_operation)

        try:
            node = self.config.node(plan["node_id"])
            project = self.config.project(plan["project_id"])
            root = expand_root(node, project_root(self.config, project, node, plan["root_id"], self.store), self.runner)
            if normalized(root) != normalized(plan["root"]):
                raise Conflict("root identity changed since planning")
            # Take the durable guard immediately after the atomic claim: a crash
            # in between is then provably pre-dispatch, and a live applier is
            # visible to a second --resume attempt.
            resume_owner = plan_id if previous in {"unknown", "applying"} else None
            guards.extend(self.ledger.acquire_guards(
                node.id, [root], operation=operation, owner_id=plan_id, resume_owner=resume_owner,
            ))
            held_roots.add((self.ledger.physical_node(node.id), normalized(root)))
            if recovering and plan["action"] == "cleanup":
                # Recover exclusion from the durable owner, not from age. Old
                # target-side calls are fenced before relying on absence below.
                for guard in self.ledger.guards():
                    if guard.get("owner_id") == plan_id and guard.get("operation") == operation:
                        guards.append(guard["guard_id"])
                        held_roots.update((self.ledger.physical_node(guard["node_id"]), normalized(p)) for p in guard["roots"])
                guards = list(dict.fromkeys(guards))
            save("applying", stage="guarded")
            if parse_iso(plan["expires_at"]) < now_utc() and not recovering:
                raise Conflict("storage plan expired; review a fresh plan")
            if reference_fingerprint(self.config, self.store, self.ledger) != plan["reference_fingerprint"]:
                raise Conflict("references, pins, ownership or configuration changed; review a fresh storage plan")
            if plan["action"] == "cleanup" and plan.get("source_node_id"):
                source = self.config.node(plan["source_node_id"])
                source_root = expand_root(source, project_root(self.config, project, source, "code", self.store), self.runner)
                guards.extend(self.ledger.acquire_guards(
                    source.id, [source_root], operation=operation, owner_id=plan_id,
                    resume_owner=resume_owner,
                ))
                listing = self.fs.scan(source, source_root,
                                       max_entries=self.config.settings.storage.max_entries,
                                       max_seconds=self.config.settings.storage.max_scan_seconds,
                                       excludes=CODE_EXCLUDES)
                if not listing.get("complete") or any(e["path"] in _entries(listing) for e in plan["entries"]):
                    raise Conflict("source reappeared or could not be checked; refuse stale-code cleanup")
            # Retained copies are guarded too; pin/reference changes use the same root guards.
            for entry in plan["entries"]:
                replica = entry.get("retained_copy")
                if replica:
                    key = (self.ledger.physical_node(replica["node_id"]), normalized(replica["root"]))
                    if key not in held_roots and (key[0] != self.ledger.physical_node(node.id) or not overlaps(root, replica["root"])):
                        guards.extend(self.ledger.acquire_guards(
                            replica["node_id"], [replica["root"]], operation=operation,
                            owner_id=plan_id, resume_owner=resume_owner,
                        ))
                        held_roots.add(key)
            target_operation = None
            if plan["action"] == "cleanup" and plan["entries"]:
                # Quiesce/fence the target helper before interpreting missing
                # paths or validating retained copies after a controller crash.
                target_operation = self.fs.prepare_delete(node, root, plan_id)
                save("applying", stage="target_prepared", target_operation=target_operation)
            paths = [e["path"] for e in plan["entries"]]
            inspected = self.fs.inspect(node, root, paths, hashes=True) if paths else {"complete": True, "entries": [], "missing": []}
            missing = set(inspected.get("missing", []))
            errors = [e for e in inspected.get("errors", [])
                      if not (isinstance(e, dict) and e.get("path") in missing)]
            # On an interrupted attempt, a missing file is evidence it was already
            # removed. A fresh/partial plan may not silently reinterpret a missing
            # file as success.
            allowed_missing = recovering and plan["action"] == "cleanup"
            if (not inspected.get("complete") and not (allowed_missing and not errors)) or errors:
                raise Conflict("planned files changed/disappeared or could not be revalidated",
                               detail={"errors": inspected.get("errors")})
            actual = _entries(inspected)
            remaining = []
            for entry in plan["entries"]:
                if entry["path"] in missing:
                    remaining.append(entry)
                    continue
                current_file = actual.get(entry["path"])
                if not current_file or not _match(entry, current_file):
                    raise Conflict("planned file fingerprint changed", detail={"path": entry["path"]})
                if plan["action"] == "cleanup":
                    reasons = protection_reasons(self.config, self.store, self.ledger, project.id, node.id,
                                                 root, {**current_file, "owner_id": entry.get("owner_id")}, run_id=plan.get("run_id"),
                                                 runner=self.runner)
                    if reasons:
                        raise Conflict("file is now protected", detail={"path": entry["path"], "reasons": reasons})
                    replica = entry.get("retained_copy")
                    if replica:
                        target = NodeSpec(id=CONTROLLER_NODE, kind="local") if replica["node_id"] == CONTROLLER_NODE else self.config.node(replica["node_id"])
                        check = self.fs.inspect(target, replica["root"], [entry["path"]], hashes=True)
                        if not check.get("complete") or not _match(entry, _entries(check).get(entry["path"], {})):
                            raise Conflict("retained copy no longer validates", detail={"path": entry["path"]})
                remaining.append(entry)
            save("applying", stage="validated")
            for entry in plan["entries"]:
                self.ledger.journal(plan_id, entry["path"], "prepared")
            if plan["action"] == "adopt":
                self.ledger.record_owned(
                    project.id, node.id, normalized(root), remaining,
                    kind=plan["kind"], owner_id=plan["owner_id"],
                    source_node=plan.get("source_node_id"),
                    source_root=plan.get("source_root"),
                )
                report = {"plan_id": plan_id, "status": "completed", "registered": len(remaining),
                          "deleted": [], "note": "Registration only; deletion requires a separate reviewed plan."}
            else:
                keep_guards = True
                dispatched = True
                save("applying", stage="dispatching")
                report = self.fs.delete(node, root, remaining, operation=target_operation) if remaining else {
                    "deleted": [], "missing": [], "blocked": [], "errors": [], "logical_deleted_bytes": 0,
                }
                blocked = report.get("blocked", []) or report.get("errors", [])
                accounted = set(report.get("deleted", [])) | set(report.get("missing", []))
                all_done = not blocked and all(entry["path"] in accounted for entry in remaining)
                report.update(plan_id=plan_id, status="completed" if all_done else "partial",
                              estimated_reclaim_bytes=plan.get("estimated_reclaim_bytes"),
                              note="Logical bytes removed; physical disk/quota reclaim may differ.")
                for path in report.get("deleted", []):
                    self.ledger.journal(plan_id, path, "deleted")
                for item in report.get("blocked", []):
                    self.ledger.journal(plan_id, item["path"], "blocked", item)
                for path in report.get("missing", []):
                    self.ledger.journal(plan_id, path, "missing")
                for path in missing:
                    self.ledger.journal(plan_id, path, "already_missing")
            if plan["action"] == "cleanup":
                save("applying", report=report, stage="postchecking")
                report["postcheck"] = self.fs.inspect(node, root, paths, hashes=False)
                if report["status"] == "completed":
                    verified_absent = set(report["postcheck"].get("missing", []))
                    if any(path not in verified_absent for path in paths):
                        raise Conflict("postcheck cannot confirm all selected paths are absent; keep guards")
            self._audit(operation, node_id=node.id, detail=report)
            save(report["status"], report=report, stage=report["status"])
            keep_guards = False
            return report
        except BaseException as exc:
            # Set the in-memory safety gate BEFORE any fallible database call.
            # SIGINT or a failed unknown-state write cannot release exclusion.
            keep_guards = keep_guards or dispatched or recovering
            try:
                save("unknown" if keep_guards else "planned",
                     report={"error": str(exc)[:500], "guards": guards},
                     stage="outcome_unknown" if keep_guards else "preflight_blocked")
                self.store.record_audit(
                    actor=self.actor, operation=operation, tier="execute", decision="error",
                    node_id=plan["node_id"], detail={"plan_id": plan_id, "error": str(exc)[:400],
                                                   "dispatched": dispatched},
                )
            except Exception:
                pass  # Preserve the original failure and persistent guards.
            raise
        finally:
            if not keep_guards:
                for token in reversed(guards):
                    self.ledger.release_guard(token)

    def _audit(self, operation: str, *, node_id=None, run_id=None, detail=None):
        self.store.record_audit(actor=self.actor, operation=operation, tier="execute", decision="allowed",
                                node_id=node_id, run_id=run_id, detail=detail or {})
