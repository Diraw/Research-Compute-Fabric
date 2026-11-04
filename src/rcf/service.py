"""The service facade shared by the CLI and the MCP server.

Every operation returns plain JSON-compatible data.  That is deliberate: a test
asserts that the CLI and the MCP server expose the *same* operations through
this one object, so an Agent and a human cannot drift apart.
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from .core.config import Config, default_home
from .core.errors import NotFound, RcfError, UnsupportedOperation
from .core.ids import new_id, now_utc
from .core.models import (
    EnvironmentPlan,
    EnvironmentView,
    NodeKind,
    NodeSpec,
    ResourceSnapshot,
    RunPlan,
    RunRecord,
    RunRequest,
    RunState,
)
from .core.runner import Runner, SubprocessRunner
from .environments.manager import EnvironmentManager
from .projects.service import ProjectService
from .resources.inspector import ResourceInspector, gpu_key
from .runs.manager import RunManager
from .runs.planner import RunPlanner
from .runs.store import Store
from .security.redaction import redact


class RcfService:
    def __init__(
        self,
        config: Config | None = None,
        *,
        home: Path | str | None = None,
        runner: Runner | None = None,
        actor: str = "api",
    ) -> None:
        self.config = config or Config.load(home)
        self.runner: Runner = runner or SubprocessRunner()
        self.actor = actor
        self._store: Store | None = None
        self._inspector: ResourceInspector | None = None
        self._environments: EnvironmentManager | None = None
        self._projects: ProjectService | None = None
        self._planner: RunPlanner | None = None
        self._runs: RunManager | None = None
        self._storage = None
        self._progress = None

    # -- lazily built collaborators ----------------------------------------- #
    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.config.db_path)
        return self._store

    @property
    def inspector(self) -> ResourceInspector:
        if self._inspector is None:
            self._inspector = ResourceInspector(self.config, self.runner)
        return self._inspector

    @property
    def environments(self) -> EnvironmentManager:
        if self._environments is None:
            self._environments = EnvironmentManager(self.config, self.runner, store=self.store)
        return self._environments

    @property
    def projects(self) -> ProjectService:
        if self._projects is None:
            self._projects = ProjectService(self.config, self.runner)
        return self._projects

    @property
    def planner(self) -> RunPlanner:
        if self._planner is None:
            self._planner = RunPlanner(
                self.config, self.store, self.inspector, self.environments, self.projects
            )
        return self._planner

    @property
    def runs(self) -> RunManager:
        if self._runs is None:
            self._runs = RunManager(self.config, self.store, self.planner, self.runner)
        return self._runs

    def _run_payload(self, record: RunRecord) -> dict[str, Any]:
        """Run record plus how fresh its state claim is.

        ``state`` alone cannot tell a live run from a cached one: when a probe
        fails RCF keeps the last known state on purpose.  Every reader (CLI,
        MCP, a dashboard) therefore gets ``observed_age_seconds`` and ``stale``
        so it never presents a three-day-old claim as "running".
        """

        payload = record.model_dump(mode="json")
        stale_after = self.config.settings.runs.stale_after_seconds
        # Age of the *claim*, not of the last attempt: a failed probe refreshes
        # last_observed_at, and using it would make a dead node look healthy.
        basis = record.state_observed_at or record.submitted_at
        if basis is None:
            # Nothing has claimed anything about a node yet (e.g. a plan that
            # was never submitted): not stale, just unobserved.
            payload["observed_age_seconds"] = None
            payload["stale"] = False
            return payload
        age = (now_utc() - basis).total_seconds()
        payload["observed_age_seconds"] = round(max(age, 0.0), 3)
        payload["stale"] = (not record.state.terminal) and age > stale_after
        payload["stale_after_seconds"] = stale_after
        return payload

    @staticmethod
    def _latest_diagnosis(events: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the most recent structured failure diagnosis, if any.

        Diagnosis events are written when a run reaches a terminal failure.
        Lifting the latest one to the top level means a reader does not have to
        scan the event list to answer "why did it fail?"; the full events stay
        available unchanged.
        """

        for event in reversed(events):
            if event.get("kind") == "diagnosis":
                return {
                    "at": event.get("at"),
                    "message": event.get("message", ""),
                    "data": event.get("data") or {},
                }
        return None

    @property
    def storage(self):
        from .storage.manager import StorageManager

        if self._storage is None:
            self._storage = StorageManager(self.config, self.store, self.runner, actor=self.actor)
        return self._storage

    def scan_storage(self, project_id: str, node_id: str) -> dict[str, Any]:
        return self.storage.scan(project_id, node_id)

    def report_storage(self, project_id: str, node_id: str | None = None, *, largest: int = 20) -> dict[str, Any]:
        return self.storage.report(project_id, node_id, largest=largest)

    def check_storage(self, project_id: str, node_id: str | None = None) -> dict[str, Any]:
        return self.storage.check(project_id, node_id)

    def plan_storage_cleanup(self, project_id: str, node_id: str, *, kind: str = "disposable",
                             root_id: str | None = None, source_node_id: str | None = None,
                             run_id: str | None = None) -> dict[str, Any]:
        return self.storage.plan_cleanup(project_id, node_id, kind=kind, root_id=root_id,
                                          source_node_id=source_node_id, run_id=run_id)

    def plan_storage_adoption(self, project_id: str, node_id: str, *, root_id: str, paths: list[str],
                              kind: str = "output", run_id: str | None = None,
                              source_node_id: str | None = None) -> dict[str, Any]:
        return self.storage.plan_adoption(
            project_id, node_id, root_id=root_id, paths=paths, kind=kind, run_id=run_id,
            source_node_id=source_node_id,
        )

    def reconcile_storage_plan(self, plan_id: str, *, confirm: bool = False) -> dict[str, Any]:
        return self.storage.reconcile(plan_id, confirm=confirm)

    def get_storage_plan(self, plan_id: str) -> dict[str, Any]:
        return self.storage.show_plan(plan_id)

    def apply_storage_plan(
        self, plan_id: str, *, confirm: bool = False, resume: bool = False,
    ) -> dict[str, Any]:
        return self.storage.apply(plan_id, confirm=confirm, resume=resume)

    def pin_run(self, run_id: str, *, pinned: bool = True, reason: str = "",
                confirm: bool = False) -> dict[str, Any]:
        return self.storage.pin(run_id, pinned=pinned, reason=reason, confirm=confirm)

    @property
    def progress(self):
        from .progress.manager import ProgressManager

        if self._progress is None:
            self._progress = ProgressManager(self.config, self.store, self.runner, self.runs)
        return self._progress

    def get_run_progress(self, run_id: str, *, refresh_state: bool = True) -> dict[str, Any]:
        self.config.policy().check("run.progress")
        if refresh_state:
            self.runs.refresh(run_id)
        return self.progress.observe(run_id)

    def wait_run(self, run_id: str, *, timeout_seconds: float = 30,
                 after_revision: str | None = None, until: str = "change") -> dict[str, Any]:
        self.config.policy().check("run.wait")
        return self.progress.wait(run_id, timeout_seconds=timeout_seconds,
                                  after_revision=after_revision, until=until)

    # -- nodes -------------------------------------------------------------- #
    def list_nodes(self) -> list[dict[str, Any]]:
        results = []
        for node in sorted(self.config.nodes.values(), key=lambda n: n.id):
            results.append(
                {
                    "id": node.id,
                    "name": node.display_name,
                    "kind": node.kind.value,
                    "labels": node.labels,
                    "work_root": node.work_root,
                    "ssh_alias": node.ssh_alias,
                    "gpus_expected": node.gpus_expected,
                    "partitions": node.slurm.partitions if node.slurm else [],
                    "default_partition": node.slurm.default_partition if node.slurm else None,
                    "notes": node.notes,
                }
            )
        return results

    def node(self, node_id: str) -> NodeSpec:
        return self.config.node(node_id)

    def selftest_node(self, node_id: str) -> dict[str, Any]:
        """Prove the control path to a node works (no job is submitted)."""

        from .resources.selftest import NodeSelfTest

        node = self.config.node(node_id)
        outcome = NodeSelfTest(self.config, self.runner).run(node)
        self.store.record_audit(
            actor=self.actor,
            operation="node.selftest",
            tier="execute",
            decision="allowed" if outcome.ok else "failed",
            node_id=node.id,
            detail={"checks": [c.name for c in outcome.checks if c.status.value == "fail"]},
        )
        return outcome.to_dict()

    def inspect_node(self, node_id: str, *, refresh: bool = False) -> dict[str, Any]:
        node = self.config.node(node_id)
        snapshot = self.inspector.inspect(node, refresh=refresh)
        assessments = self.inspector.assess_gpus(
            node, snapshot, leases=self.store.active_leases(node.id)
        )
        leases = self.store.active_leases(node.id)
        return {
            "node": {
                "id": node.id,
                "kind": node.kind.value,
                "labels": node.labels,
                "work_root": node.work_root,
                "ssh_alias": node.ssh_alias,
            },
            "snapshot": snapshot.model_dump(mode="json"),
            "gpu_schedulability": [a.model_dump(mode="json") for a in assessments],
            "active_leases": leases,
            "summary": self._snapshot_summary(snapshot),
        }

    @staticmethod
    def _snapshot_summary(snapshot: ResourceSnapshot) -> dict[str, Any]:
        gpu_free = snapshot.gpu_free_memory_mb()
        return {
            "observed_at": snapshot.observed_at.isoformat(),
            "adapter": snapshot.adapter,
            "cpus": snapshot.cpu.logical_cores if snapshot.cpu else None,
            "load_1m": snapshot.cpu.load_1m if snapshot.cpu else None,
            "memory_total_mb": snapshot.memory.total_mb if snapshot.memory else None,
            "memory_available_mb": snapshot.memory.available_mb if snapshot.memory else None,
            "gpus": len(snapshot.gpus),
            "gpu_free_mb": gpu_free,
            "partitions": [p.name for p in snapshot.scheduler.partitions],
            "errors": snapshot.errors,
        }

    # -- environments ------------------------------------------------------- #
    def list_environments(self, *, node_id: str | None = None) -> list[dict[str, Any]]:
        views: list[EnvironmentView] = []
        nodes = [self.config.node(node_id)] if node_id else list(self.config.nodes.values())
        for env in sorted(self.config.environments.values(), key=lambda e: e.id):
            for node in sorted(nodes, key=lambda n: n.id):
                # Environments are per-node: without a path there is nothing to
                # inspect (a system interpreter still needs an explicit entry).
                if not env.path_for(node.id):
                    continue
                views.append(self.environments.inspect(env, node))
        return [v.model_dump(mode="json") for v in views]

    def inspect_environment(
        self, env_id: str, node_id: str, *, verify: bool = False, confirm: bool = False,
    ) -> dict[str, Any]:
        # Legacy callers may request verification, but receive the same explicit
        # execution gate as the separate non-read-only verification endpoint.
        if verify:
            return self.verify_environment(env_id, node_id, confirm=confirm)
        env = self.config.environment(env_id)
        node = self.config.node(node_id)
        return self.environments.inspect(env, node).model_dump(mode="json")

    def verify_environment(self, env_id: str, node_id: str, *, confirm: bool = False) -> dict[str, Any]:
        policy = self.config.policy()
        policy.check("environment.verify")
        if not confirm:
            from .core.errors import PermissionDenied

            raise PermissionDenied("environment.verify requires explicit confirmation")
        env = self.config.environment(env_id)
        node = self.config.node(node_id)
        view = self.environments.inspect(env, node, verify=True, confirmed=confirm)
        self.store.record_audit(
            actor=self.actor, operation="environment.verify", tier="execute", decision="allowed",
            node_id=node.id, detail={"env_id": env.id, "state": view.state.value},
        )
        return view.model_dump(mode="json")

    def plan_environment(self, env_id: str, node_id: str) -> dict[str, Any]:
        env = self.config.environment(env_id)
        node = self.config.node(node_id)
        plan = self.environments.plan(env, node)
        return plan.model_dump(mode="json")

    # -- project push ------------------------------------------------------- #
    def push_project_plan(
        self,
        project_id: str,
        node_id: str,
        *,
        source_node_id: str | None = None,
        force: bool = False,
        scope: str = "code",
    ) -> dict[str, Any]:
        project = self.config.project(project_id)
        target = self.config.node(node_id)
        source = self._push_source(project, target, source_node_id, scope=scope)
        plan = self.projects.push_plan(project, target, source=source, force=force, scope=scope)
        return plan.model_dump(mode="json")

    def push_project(
        self,
        project_id: str,
        node_id: str,
        *,
        source_node_id: str | None = None,
        force: bool = False,
        confirm: bool = False,
        scope: str = "code",
    ) -> dict[str, Any]:
        project = self.config.project(project_id)
        target = self.config.node(node_id)
        source = self._push_source(project, target, source_node_id, scope=scope)
        plan = self.projects.push_plan(project, target, source=source, force=force, scope=scope)
        self.projects.policy.check_confirmation("project.push", confirm)
        self.store.record_audit(
            actor=self.actor,
            operation="project.push",
            tier="execute",
            decision="attempted",
            node_id=target.id,
            detail={
                "project_id": project.id,
                "source_node": source.id,
                "scope": scope,
                "files": plan.pending,
                "bytes": plan.bytes,
            },
        )
        from .storage.roots import expand_root

        target_root = expand_root(target, plan.target_root, self.runner)
        source_root = expand_root(source, plan.source_root, self.runner)
        guards = self.storage.ledger.acquire_guards(
            target.id, [target_root], operation="push", owner_id=project.id,
        )
        try:
            guards.extend(self.storage.ledger.acquire_guards(
                source.id, [source_root], operation="push-source", owner_id=project.id,
            ))
            report = self.projects.push_apply(project, target, source=source, plan=plan)
            if report.verified and report.entries:
                entries = [{"path": e.path, "bytes": e.size, "sha256": e.sha256} for e in report.entries]
                from .storage.roots import normalized

                self.storage.ledger.record_owned(project.id, target.id, normalized(target_root), entries,
                                                 kind="code" if scope == "code" else "input",
                                                 owner_id=new_id("deploy"), source_node=source.id,
                                                 source_root=source_root)
        finally:
            for token in reversed(guards):
                self.storage.ledger.release_guard(token)
        result = report.model_dump(mode="json")
        result["storage_warnings"] = self.storage.check(project.id, target.id)["warnings"]
        return result

    def _push_source(self, project, target, source_node_id: str | None, *, scope: str = "code"):
        """Where the files come from: an explicit node, or the local checkout."""

        def has_root(node) -> bool:
            return bool(
                project.data_path_for(node.id) if scope == "data" else project.path_for(node.id)
            )

        if source_node_id:
            return self.config.node(source_node_id)
        for node in self.config.nodes.values():
            if node.id != target.id and node.kind is NodeKind.LOCAL and has_root(node):
                return node
        field = "data_paths" if scope == "data" else "paths"
        raise NotFound(
            f"no local {scope} root of {project.id!r} to push from",
            hint=f"pass --from <node>, or declare [projects.{project.id}.{field}] for a local node",
            detail={"project_id": project.id, "target": target.id},
        )

    def lock_environment(self, env_id: str, node_id: str) -> dict[str, Any]:
        """A rebuildable package list for one environment on one node."""

        env = self.config.environment(env_id)
        node = self.config.node(node_id)
        text = self.environments.lock(env, node)
        return {"env_id": env.id, "node_id": node.id, "path": env.path_for(node.id), "lock": text}

    def apply_environment(self, env_id: str, node_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        env = self.config.environment(env_id)
        node = self.config.node(node_id)
        plan: EnvironmentPlan = self.environments.plan(env, node)
        # Record the tier the policy actually enforced (EXECUTE) and the one
        # piece of evidence that decided it.  "environment_mutation" is not a
        # gate for this operation, so auditing it as such misleads both humans
        # and Agents into looking for a boolean that does not matter.
        self.store.record_audit(
            actor=self.actor,
            operation="environment.apply",
            tier="execute",
            decision="attempted",
            node_id=node.id,
            detail={
                "env_id": env.id,
                "steps": [s.command for s in plan.steps],
                "gate": "path ownership + explicit confirmation",
                "action": plan.action,
                "requires_authorization": plan.requires_authorization,
            },
        )
        results = self.environments.apply(plan, env, node, confirmed=confirmed)
        return {
            "env_id": env.id,
            "node_id": node.id,
            "plan": plan.model_dump(mode="json"),
            "results": results,
        }

    # -- projects ----------------------------------------------------------- #
    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "id": project.id,
                "name": project.name or project.id,
                "git_url": project.git_url,
                "commit": project.commit,
                "env_id": project.env_id,
                "entrypoint": project.entrypoint,
                "paths": project.paths,
                "required_labels": project.required_labels,
                "description": project.description,
            }
            for project in sorted(self.config.projects.values(), key=lambda p: p.id)
        ]

    def inspect_project(self, project_id: str, node_id: str, *, sync_plan: bool = False) -> dict[str, Any]:
        project = self.config.project(project_id)
        node = self.config.node(node_id)
        view = self.projects.view(project, node)
        payload: dict[str, Any] = {"project": view.model_dump(mode="json")}
        if sync_plan:
            payload["sync_plan"] = self.projects.sync_plan(project, node, view=view).model_dump(mode="json")
        return payload

    def sync_project(
        self, project_id: str, node_id: str, *, apply: bool = False, confirmed: bool = False
    ) -> dict[str, Any]:
        project = self.config.project(project_id)
        node = self.config.node(node_id)
        plan = self.projects.sync_plan(project, node)
        payload: dict[str, Any] = {"plan": plan.model_dump(mode="json"), "applied": False}
        if not apply:
            return payload
        results = self.projects.sync_apply(plan, node, confirmed=confirmed)
        payload["applied"] = True
        payload["results"] = results
        self.store.record_audit(
            actor=self.actor,
            operation="project.sync",
            tier="execute",
            decision="allowed",
            node_id=node.id,
            detail={"project_id": project.id, "target": plan.target_commit, "results": results},
        )
        return payload

    # -- runs --------------------------------------------------------------- #
    def plan_run(self, request: RunRequest | dict[str, Any]) -> dict[str, Any]:
        payload = request if isinstance(request, RunRequest) else RunRequest.model_validate(request)
        plan = self.runs.plan(payload)
        result = plan.model_dump(mode="json")
        # Cached, lightweight nudge; planning never recursively scans a node.
        result["storage_warnings"] = self.storage.check(
            plan.request.project_id, plan.node_id,
        )["warnings"]
        return result

    def submit_run(
        self,
        plan_id: str,
        *,
        idempotency_key: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        record = self.runs.submit(
            plan_id, idempotency_key=idempotency_key, confirmed=confirm, actor=self.actor
        )
        return self._run_payload(record)

    def get_run(self, run_id: str, *, refresh: bool = True) -> dict[str, Any]:
        record = self.runs.refresh(run_id) if refresh else self.runs.get(run_id)
        payload = self._run_payload(record)
        payload["events"] = [e.model_dump(mode="json") for e in self.runs.events(run_id, limit=50)]
        payload["diagnosis"] = self._latest_diagnosis(payload["events"])
        from .progress.store import ProgressStore

        plan = self.store.get_plan(record.plan_id) if record.plan_id else None
        if refresh and plan and plan.progress_contract:
            try:
                payload["progress"] = self.get_run_progress(run_id, refresh_state=False)
            except Exception as exc:
                payload["progress"] = {"eta_seconds": None, "reason": f"progress unavailable: {exc}"}
        else:
            payload["progress"] = ProgressStore(self.store).latest(run_id)
        return payload

    def list_runs(self, *, limit: int = 20, state: str | None = None) -> list[dict[str, Any]]:
        parsed = RunState(state) if state else None
        return [self._run_payload(r) for r in self.runs.list(limit=limit, state=parsed)]

    def get_logs(self, run_id: str, *, tail: int = 200) -> dict[str, Any]:
        text = self.runs.logs(run_id, tail=tail)
        record = self.runs.get(run_id)
        return {
            "run_id": run_id,
            "state": record.state.value,
            "tail": tail,
            "log": redact(text),
        }

    def cancel_run(self, run_id: str, *, confirm: bool = False) -> dict[str, Any]:
        return self._run_payload(self.runs.cancel(run_id, confirmed=confirm, actor=self.actor))

    def fetch_results(
        self, run_id: str, *, dest: str, force: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Copy a run's artifacts to this machine (EXECUTE tier, verified)."""

        from .runs.fetch import ResultFetcher

        record = self.runs.get(run_id)
        fetcher = ResultFetcher(self.config, self.store, self.runner)
        from .storage.roots import CONTROLLER_NODE, expand_root

        node = self.config.node(record.node_id)
        destination = str(Path(dest).expanduser().absolute())
        source_root = expand_root(node, record.result_dir or record.workdir, self.runner)
        guards = self.storage.ledger.acquire_guards(
            node.id, [source_root], operation="fetch", owner_id=record.run_id,
        )
        try:
            guards.extend(self.storage.ledger.acquire_guards(
                CONTROLLER_NODE, [destination], operation="fetch-dest", owner_id=record.run_id,
            ))
            result = fetcher.fetch(record, dest=Path(destination), force=force, confirmed=confirm, actor=self.actor)
            receipt = result.get("copy_receipt")
            if receipt:
                receipt.update(project_id=record.project_id, run_id=record.run_id, dest_node=CONTROLLER_NODE)
                self.storage.ledger.record_copy(receipt)
        finally:
            for token in reversed(guards):
                self.storage.ledger.release_guard(token)
        result["storage_warnings"] = self.storage.check(record.project_id, record.node_id)["warnings"] if record.project_id else []
        return result

    def collect_results(self, run_id: str) -> dict[str, Any]:
        plain = self.runs.get(run_id)
        if plain.state is not RunState.SUCCEEDED:
            # Listing is harmless; but the caller should know the run is not done.
            manifest = self.runs.results(run_id)
            payload = manifest.model_dump(mode="json")
            payload["note"] = f"run state is {plain.state.value}; results may be incomplete. " + manifest.note
            return payload
        return self.runs.results(run_id).model_dump(mode="json")

    # -- meta --------------------------------------------------------------- #
    def doctor(self) -> dict[str, Any]:
        db_path = self.config.db_path
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str, *, warn_only: bool = False) -> None:
            checks.append(
                {
                    "name": name,
                    "status": "ok" if ok else ("warn" if warn_only else "fail"),
                    "detail": detail,
                }
            )

        add("python", sys.version_info >= (3, 11), f"{platform.python_version()} at {sys.executable}")
        add("ssh", bool(shutil.which("ssh")), shutil.which("ssh") or "not found on PATH")
        add("home", self.config.home.exists(), str(self.config.home), warn_only=True)
        add("database", db_path.parent.exists(), str(db_path), warn_only=not db_path.exists())
        add("nodes", bool(self.config.nodes), f"{len(self.config.nodes)} node(s) declared", warn_only=True)
        add(
            "projects",
            bool(self.config.projects),
            f"{len(self.config.projects)} project(s) declared",
            warn_only=True,
        )
        add(
            "environments",
            bool(self.config.environments),
            f"{len(self.config.environments)} environment(s) declared",
            warn_only=True,
        )
        checks.append(
            {
                "name": "security.allow_environment_mutation",
                "status": "info",
                # Printed by doctor historically; it never gated environment
                # writes.  Say so once, next to the flag, instead of letting
                # every reader infer a permission that does not exist.
                "detail": (
                    "legacy/compat field, not a gate: environment.apply requires the "
                    "execute tier plus path ownership (managed_roots or managed=true) "
                    "and an explicit confirmation"
                ),
            }
        )
        for node in self.config.nodes.values():
            add(
                f"node:{node.id}",
                node.kind is NodeKind.LOCAL or bool(node.ssh_alias),
                f"kind={node.kind.value} ssh_alias={node.ssh_alias or '-'}",
            )
        return {
            "ok": all(c["status"] != "fail" for c in checks),
            "checks": checks,
            "config_files": [str(p) for p in self.config.loaded_files],
            "warnings": self.config.warnings,
            "security": self.config.policy().to_dict(),
            "database": str(db_path),
            "home": str(self.config.home),
        }

    def summary(self) -> dict[str, Any]:
        runs = self.store.list_runs(limit=200)
        by_state: dict[str, int] = {}
        for record in runs:
            by_state[record.state.value] = by_state.get(record.state.value, 0) + 1
        return {
            "home": str(self.config.home),
            "nodes": len(self.config.nodes),
            "projects": len(self.config.projects),
            "environments": len(self.config.environments),
            "runs": by_state,
            "leases": self.store.active_leases(),
        }

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> RcfService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_service(home: str | None = None, *, actor: str = "api") -> RcfService:
    return RcfService(Config.load(home or default_home()), actor=actor)


def error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RcfError):
        return exc.to_dict()
    return {"code": "internal_error", "message": redact(str(exc))}


__all__ = [
    "RcfService",
    "open_service",
    "error_payload",
    "NotFound",
    "UnsupportedOperation",
    "gpu_key",
    "RunPlan",
    "RunRecord",
]
