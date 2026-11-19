"""The service facade shared by the CLI and the MCP server.

Every operation returns plain JSON-compatible data.  That is deliberate: a test
asserts that the CLI and the MCP server expose the *same* operations through
this one object, so an Agent and a human cannot drift apart.
"""

from __future__ import annotations

import platform
import re
import shutil
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .core.config import Config, default_home
from .core.errors import Conflict, NotFound, RcfError, SpecError, UnsupportedOperation
from .core.ids import new_id, now_utc
from .core.models import (
    EnvironmentPlan,
    EnvironmentSpec,
    EnvironmentView,
    FailureKind,
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
from .runs.diff import compare_runs
from .runs.manager import RunManager, state_age_seconds
from .runs.manifest import build_manifest, wall_seconds
from .runs.planner import RunPlanner
from .runs.store import Store
from .runs.sweep import expand_grid
from .security.redaction import redact

#: Display order for failure groups: the category that needs no fix first.
_KIND_ORDER = ("domain", "environment", "input", "parameter", "code", "unknown")

_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "lost"}

_SINCE_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(value: str | None) -> int | None:
    """Turn ``7d`` / ``24h`` into seconds, refusing anything ambiguous."""

    if not value:
        return None
    match = re.fullmatch(r"(\d+)([mhdw])", value.strip().lower())
    if match is None:
        raise SpecError(
            f"cannot read {value!r} as a time window",
            hint="use a number and a unit: 90m, 24h, 7d, 2w",
            detail={"value": value},
        )
    return int(match.group(1)) * _SINCE_UNITS[match.group(2)]


def _event_date(payload: dict[str, Any]) -> datetime | None:
    """When the run reached its recorded outcome, as a date-bearing timestamp."""

    for key in ("finished_at", "created_at"):
        value = payload.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            continue
    return None


def failure_trend(payloads: list[dict[str, Any]], *, max_buckets: int = 12) -> dict[str, Any]:
    """Count failures per kind per time bucket.

    Not a rate chart.  With a few dozen failures a one-run difference is noise, and
    a slope would invent a trend that the data cannot support.  What the data does
    support: which kinds appeared, in which buckets, and how many runs finished in
    each bucket so a count can be read against its denominator.
    """

    dated = [(payload, _event_date(payload)) for payload in payloads]
    dated = [(payload, moment) for payload, moment in dated if moment is not None]
    if not dated:
        return {"bucket": "day", "buckets": [], "kinds": [], "truncated": False}

    span_days = (max(m for _, m in dated) - min(m for _, m in dated)).days
    weekly = span_days > 14
    all_keys = sorted({_bucket_key(moment, weekly) for _, moment in dated})
    truncated = len(all_keys) > max_buckets
    shown = all_keys[-max_buckets:]

    per_bucket: dict[str, dict[str, Any]] = {}
    for key in all_keys:
        members = [
            payload for payload, moment in dated if _bucket_key(moment, weekly) == key
        ]
        finished = [p for p in members if p["state"] in {"succeeded", "failed"}]
        counts = {kind: 0 for kind in _KIND_ORDER}
        for payload in finished:
            if payload["state"] == "failed":
                counts[payload.get("failure_kind") or "unknown"] += 1
        per_bucket[key] = {
            "key": key,
            "label": _bucket_label(key, weekly),
            "finished": len(finished),
            "failed": sum(counts.values()),
            "counts": counts,
        }

    columns = [per_bucket[key] for key in shown]
    earlier: list[str] = []
    if truncated:
        # Fold everything before the window into one column instead of dropping it.
        # Nothing is hidden, so the reader still sees whether a kind was already
        # failing earlier without scrolling or trusting a summary line.
        earlier = all_keys[: len(all_keys) - max_buckets]
        merged = {
            "key": "earlier",
            "label": "earlier",
            "finished": sum(per_bucket[key]["finished"] for key in earlier),
            "failed": sum(per_bucket[key]["failed"] for key in earlier),
            "counts": {
                kind: sum(per_bucket[key]["counts"][kind] for key in earlier)
                for kind in _KIND_ORDER
            },
        }
        columns.insert(0, merged)

    kinds = []
    for kind in _KIND_ORDER:
        series = [column["counts"][kind] for column in columns]
        if not any(series):
            continue
        kinds.append({"kind": kind, "counts": series, "total": sum(series)})
    return {
        "bucket": "week" if weekly else "day",
        "buckets": columns,
        "kinds": kinds,
        "truncated": truncated,
        "folded_before": _bucket_label(shown[0], weekly) if truncated else None,
    }


def _bucket_key(moment: datetime, weekly: bool) -> str:
    if weekly:
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    return moment.date().isoformat()


def _bucket_label(key: str, weekly: bool) -> str:
    if weekly:
        # Short on purpose: the section says the columns are weeks, so repeating
        # "week of" in every header only makes the table wider than the page.
        year, week = key.split("-W")
        monday = datetime.fromisocalendar(int(year), int(week), 1)
        return monday.strftime("%b %d")
    return datetime.fromisoformat(key).strftime("%b %d")


def _is_terminal(state: str) -> bool:
    return state in _TERMINAL_STATES


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
        age = state_age_seconds(record)
        if age is None:
            # Nothing has claimed anything about a node yet (e.g. a plan that
            # was never submitted): not stale, just unobserved.
            payload["observed_age_seconds"] = None
            payload["stale"] = False
            return payload
        payload["observed_age_seconds"] = round(age, 3)
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
        return self._execute_environment_apply(env, node, plan, confirmed=confirmed)

    def start_apply_environment(
        self, env_id: str, node_id: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        """Start a tracked apply and return immediately.

        MCP clients impose their own tool-call timeout (the local Profer bridge
        uses 60 seconds), while a conda/pip step may legitimately run for many
        minutes.  A synchronous call would be abandoned by the client while the
        node kept installing.  The job record is written first, so a caller can
        always recover the outcome with :meth:`get_environment_apply`.
        """

        env = self.config.environment(env_id)
        node = self.config.node(node_id)
        plan: EnvironmentPlan = self.environments.plan(env, node)
        # Fail unauthorised or blocked requests in the call itself; a job that
        # only fails after the caller polls is harder to act on.
        self.environments.preflight_apply(plan, env, node, confirmed=confirmed)
        self.store.reap_stale_environment_applies()
        apply_id = new_id("envapply")
        record = self.store.create_environment_apply(
            apply_id=apply_id,
            env_id=env.id,
            node_id=node.id,
            plan=plan.model_dump(mode="json"),
        )
        thread = threading.Thread(
            target=self._run_environment_apply,
            args=(apply_id, env, node, plan),
            name=f"rcf-apply-{apply_id}",
            daemon=True,
        )
        thread.start()
        return record

    def get_environment_apply(self, apply_id: str) -> dict[str, Any]:
        record = self.store.get_environment_apply(apply_id)
        if record is None:
            raise NotFound(f"environment apply {apply_id!r} not found")
        return record

    def list_environment_applies(
        self, *, env_id: str | None = None, node_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        return self.store.list_environment_applies(env_id=env_id, node_id=node_id, limit=limit)

    def _execute_environment_apply(
        self,
        env: EnvironmentSpec,
        node: NodeSpec,
        plan: EnvironmentPlan,
        *,
        confirmed: bool,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
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
        kwargs: dict[str, Any] = {"confirmed": confirmed}
        if on_step is not None:
            # Keep the call compatible with managers that predate step callbacks;
            # only the tracked MCP apply needs incremental results.
            kwargs["on_step"] = on_step
        results = self.environments.apply(plan, env, node, **kwargs)
        return {
            "env_id": env.id,
            "node_id": node.id,
            "plan": plan.model_dump(mode="json"),
            "results": results,
        }

    def _run_environment_apply(
        self, apply_id: str, env: EnvironmentSpec, node: NodeSpec, plan: EnvironmentPlan
    ) -> None:
        store = self.store
        stop = threading.Event()

        def heartbeat() -> None:
            # A live worker refreshes updated_at; a killed control process stops,
            # so the job can be declared stale instead of "running" forever.
            while not stop.wait(60):
                try:
                    store.touch_environment_apply(apply_id)
                except Exception:  # noqa: BLE001 - heartbeat must not kill the apply
                    return

        beater = threading.Thread(
            target=heartbeat, name=f"rcf-apply-beat-{apply_id}", daemon=True
        )
        beater.start()
        try:
            payload = self._execute_environment_apply(
                env,
                node,
                plan,
                confirmed=True,
                on_step=lambda result: store.record_environment_apply_step(apply_id, result),
            )
            store.finish_environment_apply(
                apply_id, state="succeeded", results=payload["results"]
            )
            store.record_audit(
                actor=self.actor,
                operation="environment.apply",
                tier="execute",
                decision="succeeded",
                node_id=node.id,
                detail={"env_id": env.id, "apply_id": apply_id},
            )
        except Exception as exc:  # noqa: BLE001 - every failure becomes a job outcome
            error = error_payload(exc)
            try:
                store.finish_environment_apply(apply_id, state="failed", error=error)
                store.record_audit(
                    actor=self.actor,
                    operation="environment.apply",
                    tier="execute",
                    decision="failed",
                    node_id=node.id,
                    detail={"env_id": env.id, "apply_id": apply_id, "error": error},
                )
            except Exception:  # noqa: BLE001 - the node result still exists
                pass
        finally:
            stop.set()
            beater.join(timeout=1)

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

    def plan_sweep(
        self,
        request: RunRequest | dict[str, Any],
        grid: dict[str, list[Any]],
        *,
        max_runs: int | None = None,
    ) -> dict[str, Any]:
        """Plan every combination of a grid, without submitting any of them.

        Planning first is the whole point: a grid with one impossible point is
        refused here, before a single job is queued.  The node is probed once —
        the inspector caches the snapshot — so the cost is the per-combination
        checks, not the network.
        """

        combos = expand_grid(grid, **({"max_combos": max_runs} if max_runs else {}))
        base = request if isinstance(request, RunRequest) else RunRequest.model_validate(request)
        sweep_id = new_id("sweep")
        entries = []
        for combo in combos:
            merged = {**base.params, **combo}
            plan = self.plan_run(base.model_copy(update={"params": merged}))
            blocked_by = [c["name"] for c in plan["checks"] if c["status"] == "fail"]
            entries.append(
                {
                    "params": merged,
                    "plan_id": plan["plan_id"],
                    "run_id": plan["run_id"],
                    "command": plan["command"]["preview"],
                    "blocked_by": blocked_by,
                }
            )
        return {
            "sweep_id": sweep_id,
            "combinations": len(entries),
            "blocked": [entry for entry in entries if entry["blocked_by"]],
            "entries": entries,
        }

    def submit_sweep(
        self,
        sweep_id: str,
        plan_ids: list[str],
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Submit planned combinations, reporting each outcome separately.

        A failure halfway through does not roll back the jobs that already started,
        so the result names both lists rather than pretending the sweep was atomic.
        """

        submitted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for index, plan_id in enumerate(plan_ids, start=1):
            try:
                payload = self.submit_run(plan_id, confirm=confirm, sweep_id=sweep_id)
            except RcfError as exc:
                failed.append(
                    {"index": index, "plan_id": plan_id, "error": f"{exc.code}: {exc.message}"}
                )
                continue
            submitted.append(
                {
                    "index": index,
                    "plan_id": plan_id,
                    "run_id": payload["run_id"],
                    "state": payload["state"],
                }
            )
        return {"sweep_id": sweep_id, "submitted": submitted, "failed": failed}

    def submit_run(
        self,
        plan_id: str,
        *,
        idempotency_key: str | None = None,
        confirm: bool = False,
        sweep_id: str | None = None,
    ) -> dict[str, Any]:
        record = self.runs.submit(
            plan_id,
            idempotency_key=idempotency_key,
            confirmed=confirm,
            actor=self.actor,
            sweep_id=sweep_id,
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

    def build_report(
        self,
        *,
        limit: int = 200,
        project: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Assemble everything the report needs, once, from the local database.

        ``limit`` bounds how far back the page looks *after* the filters, so
        ``--project`` on a project that has not run this week still finds its runs
        instead of scanning the newest N overall and finding nothing.
        """

        since_seconds = parse_since(since)
        cutoff = now_utc() - timedelta(seconds=since_seconds) if since_seconds else None
        records = self.store.list_runs(
            limit=limit, project_id=project, created_after=cutoff
        )
        payloads = [self._run_payload(record) for record in records]
        ids = [record.run_id for record in records]
        diagnoses = self.store.latest_events(ids, "diagnosis")
        # A run classified after the fact has a ``classified`` event instead of a
        # diagnosis written at failure time; both are evidence, and leaving the
        # older runs blank would read as "nothing was ever concluded".
        classified = self.store.latest_events(ids, "classified")
        for payload in payloads:
            event = diagnoses.get(payload["run_id"]) or classified.get(payload["run_id"])
            payload["evidence"] = event.message[:180] if event is not None else None

        failed = [payload for payload in payloads if payload["state"] == "failed"]
        groups = []
        for kind in _KIND_ORDER:
            # A failure recorded before classification existed has no kind; it is
            # reported as ``unknown`` rather than dropped from the page.
            members = [
                payload for payload in failed if (payload.get("failure_kind") or "unknown") == kind
            ]
            if members:
                groups.append(
                    {
                        "kind": kind,
                        "count": len(members),
                        "share": len(members) / len(failed) if failed else 0.0,
                        "runs": members,
                    }
                )

        durations: dict[str, list[float]] = {}
        for payload in payloads:
            if payload["state"] != "succeeded":
                continue
            seconds = wall_seconds(payload.get("submitted_at"), payload.get("finished_at"))
            if seconds is not None:
                durations.setdefault(payload.get("project_id") or "-", []).append(seconds)

        projects = []
        for project_id in sorted({payload.get("project_id") or "-" for payload in payloads}):
            members = [p for p in payloads if (p.get("project_id") or "-") == project_id]
            succeeded = sum(1 for p in members if p["state"] == "succeeded")
            # Cancellations are a decision, not an attempt that went wrong, so they
            # stay out of the denominator; otherwise a week of experiments looks
            # worse the more often the operator changes their mind.
            finished = [p for p in members if p["state"] in {"succeeded", "failed"}]
            series = sorted(durations.get(project_id, []))
            projects.append(
                {
                    "project_id": project_id,
                    "total": len(members),
                    "succeeded": succeeded,
                    "failed": sum(1 for p in members if p["state"] == "failed"),
                    "success_rate": succeeded / len(finished) if finished else 0.0,
                    "p50_duration_s": series[len(series) // 2] if series else None,
                    "max_duration_s": series[-1] if series else None,
                }
            )

        return {
            "generated_at": now_utc().isoformat(timespec="seconds"),
            "home": str(self.config.home),
            "filters": {"limit": limit, "project": project, "since": since},
            "counts": {
                "total": len(payloads),
                "succeeded": sum(1 for p in payloads if p["state"] == "succeeded"),
                "failed": len(failed),
                "cancelled": sum(1 for p in payloads if p["state"] == "cancelled"),
                "active": sum(1 for p in payloads if not _is_terminal(p["state"])),
            },
            "active": [p for p in payloads if not _is_terminal(p["state"])],
            "failures": groups,
            "trend": failure_trend(payloads),
            "projects": projects,
            "recent": payloads,
        }

    def run_manifest(
        self, run_id: str, *, hashes: bool = True, limit: int = 500
    ) -> dict[str, Any]:
        """Build a stable, deliverable record of one run and what it produced.

        The record is read from the database; only the output listing is live.
        When that listing cannot be read the document says so — an empty ``outputs``
        array would read as "this run produced nothing", which is a different
        claim.
        """

        payload = self._run_payload(self.runs.get(run_id))
        outputs: dict[str, Any] | None = None
        error: str | None = None
        if payload.get("handle") and payload.get("result_dir"):
            try:
                listed = self.runs.results(run_id, limit=limit, hashes=hashes)
                outputs = listed.model_dump(mode="json")
            except RcfError as exc:
                error = f"{exc.code}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - the record is still worth writing
                error = str(exc)[:200]
        return build_manifest(
            payload,
            outputs=outputs,
            outputs_error=error,
            generated_at=now_utc().isoformat(timespec="seconds"),
        )

    def diff_runs(
        self,
        left_id: str,
        right_id: str,
        *,
        artifacts: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Compare two runs field by field.

        Artifacts are opt-in because a real comparison needs content hashes: sizes
        that happen to match are not evidence of identical files, and reporting
        "same" on that basis would be worse than not comparing at all.
        """

        left = self._run_payload(self.runs.get(left_id))
        right = self._run_payload(self.runs.get(right_id))
        outputs_left = outputs_right = None
        reason: str | None = None
        if artifacts:
            try:
                outputs_left = self.runs.results(left_id, limit=limit, hashes=True).model_dump(
                    mode="json"
                )
                outputs_right = self.runs.results(right_id, limit=limit, hashes=True).model_dump(
                    mode="json"
                )
            except RcfError as exc:
                reason = f"{exc.code}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - report, never fail the diff
                reason = str(exc)[:200]
        return compare_runs(
            left,
            right,
            outputs_left=outputs_left,
            outputs_right=outputs_right,
            outputs_reason=reason,
        )

    def list_runs(
        self,
        *,
        limit: int = 20,
        state: str | None = None,
        failure_kind: str | None = None,
        sweep_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parsed = RunState(state) if state else None
        kind = FailureKind(failure_kind) if failure_kind else None
        records = self.runs.list(
            limit=limit, state=parsed, failure_kind=kind, sweep_id=sweep_id
        )
        payloads = [self._run_payload(record) for record in records]
        diagnoses = self.store.latest_events([record.run_id for record in records], "diagnosis")
        for payload in payloads:
            event = diagnoses.get(payload["run_id"])
            payload["diagnosis"] = (
                self._latest_diagnosis([event.model_dump(mode="json")]) if event else None
            )
        return payloads

    def classify_failed(
        self, *, limit: int = 40, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Classify older failures from logs that are still on the nodes."""

        return self.runs.classify_failed(limit=limit, refresh=refresh)

    def reconcile_runs(
        self, *, limit: int = 20, older_than_seconds: int | None = None
    ) -> dict[str, Any]:
        """Refresh stale runs and report what the probe changed."""

        rows = self.runs.reconcile(limit=limit, older_than_seconds=older_than_seconds)
        return {
            "checked": len(rows),
            "changed": sum(1 for row in rows if row["changed"]),
            "runs": rows,
        }

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
        from .storage.roots import CONTROLLER_NODE, expand_root, within_host

        node = self.config.node(record.node_id)
        destination = str(Path(dest).expanduser().absolute())
        source_root = expand_root(node, record.result_dir or record.workdir, self.runner)
        if node.kind is NodeKind.LOCAL:
            # Name the real problem before guard acquisition turns it into an
            # opaque "storage roots already guarded" conflict.
            guarded = [source_root]
            if record.workdir:
                guarded.append(expand_root(node, record.workdir, self.runner))
            for owned in dict.fromkeys(guarded):
                if within_host(destination, owned) or within_host(owned, destination):
                    raise Conflict(
                        "source and destination output trees must not overlap",
                        hint="choose a destination outside the run's own output/work directory",
                        detail={"dest": destination, "source": owned},
                    )
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
        local_paths: list[str] = []
        missing_paths: list[str] = []
        for project in self.config.projects.values():
            for node in self.config.nodes.values():
                if node.kind is not NodeKind.LOCAL:
                    continue
                path = project.path_for(node.id)
                if not path:
                    continue
                resolved = Path(path).expanduser()
                local_paths.append(str(resolved))
                if not resolved.exists():
                    missing_paths.append(f"{project.id}@{node.id}: {resolved}")
        checks.append(
            {
                "name": "project_paths",
                "status": "warn" if missing_paths else "ok",
                "detail": (
                    "missing local project path(s): " + "; ".join(missing_paths[:5])
                    if missing_paths
                    else f"{len(local_paths)} local project path(s) exist"
                ),
            }
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
