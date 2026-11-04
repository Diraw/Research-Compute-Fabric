"""MCP server: the Agent-facing surface of RCF.

Design rules from the brief:

* The Agent never sees hostnames, absolute paths or Slurm syntax — it names a
  project and a resource requirement.
* Planning and execution are separate tools: ``compute_plan_run`` has no side
  effects, ``compute_submit_run`` needs ``confirm=true`` and a plan id.
* Every failure is returned as structured JSON (``ok: false`` with a stable
  error code) instead of an exception the Agent has to guess about.
* Tool names use underscores because several MCP clients reject dots; the
  ``compute.*`` aliases from the design note are listed by ``TOOL_SPECS``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .. import __version__
from ..core.config import Config, default_home
from ..core.runner import SubprocessRunner
from ..service import RcfService, error_payload

SERVER_NAME = "research-compute-fabric"
INSTRUCTIONS = """\
RCF manages research compute nodes (local, SSH workstations and Slurm clusters).

Typical flow:
1. compute_list_nodes / compute_inspect_node — see what exists and what is free.
2. compute_list_projects + compute_inspect_environment — check the code and environment.
   Missing package? compute_plan_environment shows the exact commands; only then
   compute_apply_environment(confirm=true). RCF writes only prefixes it owns.
3. compute_plan_run — get a reviewable plan (node, command, checks). No side effects.
4. compute_submit_run(plan_id, confirm=true) — submit only after the user approves.
5. compute_get_run / compute_get_run_progress / compute_get_logs / compute_collect_results — follow the run.
6. compute_wait_run — bounded read-only long-poll (<=60s), not background notifications;
   callers may repeat it using the returned revision. Waiting never submits or cancels a run.

Never assume a GPU is free because VRAM looks idle: on Slurm nodes the scheduler
decides, and on SSH nodes RCF only reports evidence. Do not bypass a scheduler.
"""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    dotted: str
    description: str
    service_method: str
    cli_path: tuple[str, ...]
    read_only: bool


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="compute_list_nodes",
        dotted="compute.list_nodes",
        description="List declared compute nodes with kind, labels and SSH alias (no network access).",
        service_method="list_nodes",
        cli_path=("node", "list"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_inspect_node",
        dotted="compute.inspect_node",
        description=(
            "Probe a node read-only: CPU, memory, disk, GPUs and Slurm partitions, plus an explicit "
            "GPU schedulability assessment (free VRAM is never treated as availability)."
        ),
        service_method="inspect_node",
        cli_path=("node", "inspect"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_node_selftest",
        dotted="compute.node_selftest",
        description=(
            "Prove the control path to a node in ~10s: ssh, POSIX shell, work_root "
            "write/read/cleanup, python3 and GPU. Creates and removes one temp dir; "
            "never submits a job."
        ),
        service_method="selftest_node",
        cli_path=("node", "selftest"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_list_environments",
        dotted="compute.list_environments",
        description="Inspect declared environments per node: installed / compatible / verified, missing packages.",
        service_method="list_environments",
        cli_path=("env", "list"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_inspect_environment",
        dotted="compute.inspect_environment",
        description="Inspect one environment read-only; never run its arbitrary verification command.",
        service_method="inspect_environment",
        cli_path=("env", "show"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_verify_environment",
        dotted="compute.verify_environment",
        description="Run the configured environment verification command only with confirm=true; execution permission required.",
        service_method="verify_environment",
        cli_path=("env", "verify"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_plan_environment",
        dotted="compute.plan_environment",
        description=(
            "Plan how to create or complete an environment on one node: exact commands, missing "
            "packages, network needs and the ownership verdict. Read-only: nothing is installed "
            "and no directory is created."
        ),
        service_method="plan_environment",
        cli_path=("env", "plan"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_apply_environment",
        dotted="compute.apply_environment",
        description=(
            "Execute a reviewed environment plan (conda create/install or venv+pip). Requires "
            "confirm=true. RCF only writes prefixes it owns (managed_roots or managed=true) and "
            "refuses plans with blockers; every write is audited."
        ),
        service_method="apply_environment",
        cli_path=("env", "apply"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_list_projects",
        dotted="compute.list_projects",
        description="List research projects with entrypoint, environment and per-node paths.",
        service_method="list_projects",
        cli_path=("project", "list"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_plan_run",
        dotted="compute.plan_run",
        description=(
            "Build an execution plan for a project (node selection, environment check, GPU choice, exact "
            "command). Pure planning: nothing is started, written or reserved. Optional pilot_seconds "
            "uses a hard watchdog when submitted; runtime_budget_seconds only gates planning/alerts, "
            "never cancels a normal run. Neither walltime nor budget is an ETA."
        ),
        service_method="plan_run",
        cli_path=("run", "plan"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_submit_run",
        dotted="compute.submit_run",
        description=(
            "Submit a reviewed plan. Requires confirm=true and an executable plan; idempotency_key makes "
            "retries safe."
        ),
        service_method="submit_run",
        cli_path=("run", "submit"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_get_run",
        dotted="compute.get_run",
        description="Fetch a run's state, exit code and recent events, refreshing from the executor.",
        service_method="get_run",
        cli_path=("run", "status"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_get_run_progress",
        dotted="compute.get_run_progress",
        description="Read run progress, phase, ETA interval/scope/confidence and alerts; unknown ETA is explicit.",
        service_method="get_run_progress",
        cli_path=("run", "progress"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_wait_run",
        dotted="compute.wait_run",
        description=(
            "Bounded read-only long-poll (0 <= timeout_seconds <= 60, also capped by [progress].max_wait_seconds; zero reads cache) for change, terminal state, phase "
            "or alert. Not a background notification; repeat with after_revision. Never submits or cancels."
        ),
        service_method="wait_run",
        cli_path=("run", "wait"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_get_logs",
        dotted="compute.get_logs",
        description="Read the tail of a run's log (stdout+stderr), redacted for secrets.",
        service_method="get_logs",
        cli_path=("run", "logs"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_cancel_run",
        dotted="compute.cancel_run",
        description="Cancel a run through its executor (scancel on Slurm, process-group signal elsewhere).",
        service_method="cancel_run",
        cli_path=("run", "cancel"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_list_runs",
        dotted="compute.list_runs",
        description="List recent runs from the local database, optionally filtered by state.",
        service_method="list_runs",
        cli_path=("run", "list"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_sync_project",
        dotted="compute.sync_project",
        description=(
            "Show (or apply, with confirm=true) a git fetch/checkout for a project on one node. "
            "Refuses on a dirty working tree."
        ),
        service_method="sync_project",
        cli_path=("project", "sync"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_push_project",
        dotted="compute.push_project",
        description=(
            "Copy a project checkout to a node, sending only the files that differ. "
            "Plans by default; confirm=true applies. Never deletes, and never overwrites "
            "a file that is newer on the target unless force=true."
        ),
        service_method="push_project",
        cli_path=("project", "push"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_collect_results",
        dotted="compute.collect_results",
        description="List the result files of a run in place (no bulk transfer between nodes).",
        service_method="collect_results",
        cli_path=("run", "results"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_fetch_results",
        dotted="compute.fetch_results",
        description=(
            "Copy a run's artifacts to the control machine (tar + scp, SHA-256 verified). "
            "Writes to a local directory, so it needs confirm=true."
        ),
        service_method="fetch_results",
        cli_path=("run", "fetch"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_scan_storage",
        dotted="compute.scan_storage",
        description="Scan declared project storage roots read-only; never changes research files.",
        service_method="scan_storage",
        cli_path=("storage", "scan"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_report_storage",
        dotted="compute.report_storage",
        description="Report project storage statistics, classification and largest files.",
        service_method="report_storage",
        cli_path=("storage", "report"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_check_storage",
        dotted="compute.check_storage",
        description="Check project storage evidence and safety issues without deleting files.",
        service_method="check_storage",
        cli_path=("storage", "check"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_plan_storage_cleanup",
        dotted="compute.plan_storage_cleanup",
        description=(
            "Plan exact-file cleanup for stale-code, disposable, replica or cache. "
            "No files deleted; review paths and blockers before separately applying with confirmation."
        ),
        service_method="plan_storage_cleanup",
        cli_path=("storage", "plan"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_plan_storage_adoption",
        dotted="compute.plan_storage_adoption",
        description=(
            "Plan adoption of explicitly listed relative file paths in one root; never selects a whole "
            "root by default. Code adoption also needs source_node_id and requires a per-file hash match "
            "with that checkout. No adoption until the reviewed plan is separately applied with confirmation."
        ),
        service_method="plan_storage_adoption",
        cli_path=("storage", "adopt"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_get_storage_plan",
        dotted="compute.get_storage_plan",
        description=(
            "Read a stored storage plan with owner/stage, per-file journal and protection guards. "
            "Read-only evidence; heartbeat age does not authorize takeover or application."
        ),
        service_method="get_storage_plan",
        cli_path=("storage", "show"),
        read_only=True,
    ),
    ToolSpec(
        name="compute_reconcile_storage_plan",
        dotted="compute.reconcile_storage_plan",
        description=(
            "Observe the stored manifest and close an old storage plan as superseded, releasing its guards. "
            "Never continues deletion or resumes the plan. Requires fresh human approval and confirm=true."
        ),
        service_method="reconcile_storage_plan",
        cli_path=("storage", "reconcile"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_apply_storage_plan",
        dotted="compute.apply_storage_plan",
        description="Apply one reviewed cleanup or adoption plan. Requires fresh human approval and confirm=true.",
        service_method="apply_storage_plan",
        cli_path=("storage", "apply"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_pin_run",
        dotted="compute.pin_run",
        description=(
            "Pin or unpin a run against cleanup. Requires confirm=true; pinning requires a reason. "
            "Unpinning does not delete files."
        ),
        service_method="pin_run",
        cli_path=("run", "pin"),
        read_only=False,
    ),
    ToolSpec(
        name="compute_doctor",
        dotted="compute.doctor",
        description="Report configuration, dependency and security status of this RCF installation.",
        service_method="doctor",
        cli_path=("doctor",),
        read_only=True,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def ok(data: Any) -> str:
    return _json({"ok": True, "data": data})


def fail(exc: Exception) -> str:
    return _json({"ok": False, "error": error_payload(exc)})


class ToolRuntime:
    """Owns the single :class:`RcfService` used by all tools."""

    def __init__(self, home: str | None = None, *, actor: str = "mcp") -> None:
        self.home = home or str(default_home())
        self.actor = actor
        self._service: RcfService | None = None

    @property
    def service(self) -> RcfService:
        if self._service is None:
            self._service = RcfService(
                Config.load(self.home), runner=SubprocessRunner(), actor=self.actor
            )
        return self._service

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """Run a blocking service call off the event loop and normalise errors."""

        try:
            data = await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - every failure becomes structured data
            return fail(exc)
        return ok(data)


def build_server(home: str | None = None, *, actor: str = "mcp") -> MCPServer:
    runtime = ToolRuntime(home, actor=actor)
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def register(spec: ToolSpec, handler: Callable[..., Any]) -> None:
        server.add_tool(
            handler,
            name=spec.name,
            title=spec.dotted,
            description=spec.description,
            annotations=ToolAnnotations(readOnlyHint=spec.read_only),
            structured_output=False,
        )

    async def compute_list_nodes() -> str:
        """List declared compute nodes (no network access)."""
        return await runtime.run(runtime.service.list_nodes)

    async def compute_inspect_node(node_id: str, refresh: bool = False) -> str:
        """Probe one node read-only.

        Args:
            node_id: Node id as declared in nodes.toml.
            refresh: Bypass the short-lived inspection cache.
        """
        return await runtime.run(runtime.service.inspect_node, node_id, refresh=refresh)

    async def compute_node_selftest(node_id: str) -> str:
        """Check that a node is usable end to end (no job is submitted).

        Args:
            node_id: Node id as declared in nodes.toml.
        """
        return await runtime.run(runtime.service.selftest_node, node_id)

    async def compute_list_environments(node_id: str | None = None) -> str:
        """List declared environments and their state per node.

        Args:
            node_id: Optional node filter.
        """
        return await runtime.run(runtime.service.list_environments, node_id=node_id)

    async def compute_inspect_environment(env_id: str, node_id: str) -> str:
        """Inspect compatibility without executing a configured verification command."""
        return await runtime.run(runtime.service.inspect_environment, env_id, node_id)

    async def compute_verify_environment(env_id: str, node_id: str, confirm: bool = False) -> str:
        """Execute a reviewed verification command only after explicit approval."""
        return await runtime.run(runtime.service.verify_environment, env_id, node_id, confirm=confirm)

    async def compute_plan_environment(env_id: str, node_id: str) -> str:
        """Plan an environment install without touching the node.

        Args:
            env_id: Environment id as declared in environments.toml.
            node_id: Node the environment should live on.
        """
        return await runtime.run(runtime.service.plan_environment, env_id, node_id)

    async def compute_apply_environment(env_id: str, node_id: str, confirm: bool = False) -> str:
        """Execute a reviewed environment plan (conda/venv install or completion).

        Args:
            env_id: Environment id as declared in environments.toml.
            node_id: Node to install on.
            confirm: Must be true; asserts the plan returned by
                compute_plan_environment was reviewed.  RCF still refuses a prefix
                it does not own and any plan with blockers.
        """
        return await runtime.run(
            runtime.service.apply_environment, env_id, node_id, confirmed=confirm
        )

    async def compute_list_projects() -> str:
        """List research projects."""
        return await runtime.run(runtime.service.list_projects)

    async def compute_plan_run(
        project_id: str,
        params: dict[str, str | int | float | bool | None] | None = None,
        node_id: str | None = None,
        env_id: str | None = None,
        gpus: int = 0,
        cpus: int | None = None,
        tasks: int = 1,
        memory_gb: int | None = None,
        gpu_memory_gb: int | None = None,
        time_minutes: int | None = None,
        partition: str | None = None,
        command_override: str | None = None,
        allow_shared_gpu: bool = False,
        pilot_seconds: Annotated[int | None, Field(ge=1, le=300, strict=True)] = None,
        runtime_budget_seconds: Annotated[
            float | None, Field(gt=0, allow_inf_nan=False, strict=True)
        ] = None,
    ) -> str:
        """Build a reviewable execution plan; no side effects.

        Args:
            project_id: Project to run.
            params: Scalar parameters substituted into the entrypoint template.
            node_id: Force a specific node instead of letting RCF choose.
            env_id: Override the project's default environment.
            gpus: Number of GPUs requested.
            cpus: CPU cores requested.
            tasks: MPI ranks; the {launcher} placeholder expands to srun/mpirun.
            memory_gb: System memory in GB.
            gpu_memory_gb: Minimum VRAM per GPU in GB; lets a busy-but-large device
                be shared explicitly (with allow_shared_gpu).
            time_minutes: Walltime limit (Slurm), not an ETA.
            partition: Slurm partition override.
            command_override: Replace the project entrypoint for this run.
            allow_shared_gpu: Allow sharing a GPU already reserved by another RCF run.
            pilot_seconds: Calibration pilot seconds (1..300); hard watchdog applies on submission.
            runtime_budget_seconds: Positive planning/alert budget; no automatic cancellation of normal runs.
        """
        request = {
            "project_id": project_id,
            "params": params or {},
            "node_id": node_id,
            "env_id": env_id,
            "command_override": command_override,
            "pilot_seconds": pilot_seconds,
            "runtime_budget_seconds": runtime_budget_seconds,
            "requirements": {
                "gpus": gpus,
                "cpus": cpus,
                "tasks": tasks,
                "memory_gb": memory_gb,
                "gpu_memory_gb": gpu_memory_gb,
                "time_minutes": time_minutes,
                "partition": partition,
                "allow_shared_gpu": allow_shared_gpu,
            },
        }
        return await runtime.run(runtime.service.plan_run, request)

    async def compute_submit_run(
        plan_id: str, idempotency_key: str | None = None, confirm: bool = False
    ) -> str:
        """Submit a reviewed plan.

        Args:
            plan_id: Plan id returned by compute_plan_run.
            idempotency_key: Reusing the key returns the existing run instead of duplicating it.
            confirm: Must be true; the caller asserts a human approved this plan.
        """
        return await runtime.run(
            runtime.service.submit_run, plan_id, idempotency_key=idempotency_key, confirm=confirm
        )

    async def compute_get_run(run_id: str, refresh: bool = True) -> str:
        """Get a run's state and recent events.

        Args:
            run_id: Run id.
            refresh: Poll the executor for the current state.
        """
        return await runtime.run(runtime.service.get_run, run_id, refresh=refresh)

    async def compute_get_run_progress(run_id: str) -> str:
        """Read progress/ETA evidence without submitting or cancelling anything."""
        return await runtime.run(runtime.service.get_run_progress, run_id, refresh_state=True)

    async def compute_wait_run(
        run_id: str,
        timeout_seconds: Annotated[float, Field(ge=0, le=60, allow_inf_nan=False, strict=True)] = 30,
        after_revision: str | None = None,
        until: Literal["change", "terminal", "phase", "alert"] = "change",
    ) -> str:
        """Bounded read-only long-poll, not background notification; never submit/cancel."""
        return await runtime.run(
            runtime.service.wait_run, run_id, timeout_seconds=timeout_seconds,
            after_revision=after_revision, until=until,
        )

    async def compute_get_logs(run_id: str, tail: int = 200) -> str:
        """Read the tail of a run log.

        Args:
            run_id: Run id.
            tail: Number of lines from the end.
        """
        return await runtime.run(runtime.service.get_logs, run_id, tail=tail)

    async def compute_cancel_run(run_id: str, confirm: bool = False) -> str:
        """Cancel a run.

        Args:
            run_id: Run id.
            confirm: Must be true to actually cancel.
        """
        return await runtime.run(runtime.service.cancel_run, run_id, confirm=confirm)

    async def compute_list_runs(limit: int = 20, state: str | None = None) -> str:
        """List recent runs.

        Args:
            limit: Maximum number of runs.
            state: Optional state filter (planned, running, succeeded, failed, cancelled ...).
        """
        return await runtime.run(runtime.service.list_runs, limit=limit, state=state)

    async def compute_sync_project(
        project_id: str, node_id: str, apply: bool = False, confirm: bool = False
    ) -> str:
        """Plan or apply a git sync of a project checkout.

        Args:
            project_id: Project id.
            node_id: Node whose checkout should be updated.
            apply: Actually run git fetch/checkout.
            confirm: Must be true when apply=true.
        """
        return await runtime.run(
            runtime.service.sync_project, project_id, node_id, apply=apply, confirmed=confirm
        )

    async def compute_push_project(
        project_id: str,
        node_id: str,
        from_node: str | None = None,
        force: bool = False,
        data: bool = False,
        confirm: bool = False,
    ) -> str:
        """Plan or apply a copy of a project checkout onto a node.

        Args:
            project_id: Project id.
            node_id: Target node (where the files should land).
            from_node: Node to copy from; defaults to the local checkout.
            force: Overwrite files that are newer on the target.
            data: Copy the data root (data_paths) instead of the checkout.
            confirm: Must be true to actually copy; false returns the plan.
        """
        if confirm:
            return await runtime.run(
                runtime.service.push_project,
                project_id,
                node_id,
                source_node_id=from_node,
                force=force,
                confirm=True,
                scope="data" if data else "code",
            )
        return await runtime.run(
            runtime.service.push_project_plan,
            project_id,
            node_id,
            source_node_id=from_node,
            force=force,
            scope="data" if data else "code",
        )

    async def compute_fetch_results(
        run_id: str, dest: str, force: bool = False, confirm: bool = False
    ) -> str:
        """Copy a run's artifacts to a local directory.

        Args:
            run_id: Run id.
            dest: Local directory to write into (created if missing).
            force: Merge into a non-empty directory instead of refusing.
            confirm: Must be true; this writes to the local disk.
        """
        return await runtime.run(
            runtime.service.fetch_results, run_id, dest=dest, force=force, confirm=confirm
        )

    async def compute_collect_results(run_id: str) -> str:
        """List result files of a run (in place)."""
        return await runtime.run(runtime.service.collect_results, run_id)

    async def compute_scan_storage(project_id: str, node_id: str) -> str:
        """Scan declared storage roots without changing research files."""
        return await runtime.run(runtime.service.scan_storage, project_id, node_id)

    async def compute_report_storage(
        project_id: str, node_id: str | None = None, largest: int = 20
    ) -> str:
        """Report file statistics and largest files for a project."""
        return await runtime.run(runtime.service.report_storage, project_id, node_id, largest=largest)

    async def compute_check_storage(project_id: str, node_id: str | None = None) -> str:
        """Check storage evidence and safety issues without deleting files."""
        return await runtime.run(runtime.service.check_storage, project_id, node_id)

    async def compute_plan_storage_cleanup(
        project_id: str,
        node_id: str,
        kind: Literal["stale-code", "disposable", "replica", "cache"] = "disposable",
        root_id: str | None = None,
        source_node_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Create a cleanup plan only; nothing is deleted."""
        return await runtime.run(
            runtime.service.plan_storage_cleanup, project_id, node_id, kind=kind,
            root_id=root_id, source_node_id=source_node_id, run_id=run_id,
        )

    async def compute_plan_storage_adoption(
        project_id: str,
        node_id: str,
        root_id: str,
        paths: list[str],
        kind: Literal["code", "input", "output", "disposable", "cache"] = "output",
        run_id: str | None = None,
        source_node_id: str | None = None,
    ) -> str:
        """Plan adoption of explicit relative file paths, never an implicit whole root.

        Code adoption also needs the current source node; each selected target
        file must match that checkout exactly.
        """
        return await runtime.run(
            runtime.service.plan_storage_adoption, project_id, node_id,
            root_id=root_id, paths=paths, kind=kind, run_id=run_id,
            source_node_id=source_node_id,
        )

    async def compute_get_storage_plan(plan_id: str) -> str:
        """Read stored plan, execution ownership, journal and protection evidence only."""
        return await runtime.run(runtime.service.get_storage_plan, plan_id)

    async def compute_reconcile_storage_plan(plan_id: str, confirm: bool = False) -> str:
        """Close an old plan without deletion; requires explicit approval and never resumes it."""
        return await runtime.run(runtime.service.reconcile_storage_plan, plan_id, confirm=confirm)

    async def compute_apply_storage_plan(
        plan_id: str, confirm: bool = False, resume: bool = False,
    ) -> str:
        """Apply a reviewed storage plan only after explicit human approval.

        ``resume`` continues the exact stored file list after an interrupted
        deletion; it never widens the selection.
        """
        return await runtime.run(
            runtime.service.apply_storage_plan, plan_id, confirm=confirm, resume=resume,
        )

    async def compute_pin_run(
        run_id: str, pinned: bool = True, reason: str = "", confirm: bool = False
    ) -> str:
        """Change run retention; the service checks approval and the required pin reason."""
        return await runtime.run(
            runtime.service.pin_run, run_id, pinned=pinned, reason=reason, confirm=confirm
        )

    async def compute_doctor() -> str:
        """Report configuration and dependency status."""
        return await runtime.run(runtime.service.doctor)

    handlers: dict[str, Callable[..., Any]] = {
        "compute_list_nodes": compute_list_nodes,
        "compute_inspect_node": compute_inspect_node,
        "compute_node_selftest": compute_node_selftest,
        "compute_list_environments": compute_list_environments,
        "compute_inspect_environment": compute_inspect_environment,
        "compute_verify_environment": compute_verify_environment,
        "compute_plan_environment": compute_plan_environment,
        "compute_apply_environment": compute_apply_environment,
        "compute_list_projects": compute_list_projects,
        "compute_plan_run": compute_plan_run,
        "compute_submit_run": compute_submit_run,
        "compute_get_run": compute_get_run,
        "compute_get_run_progress": compute_get_run_progress,
        "compute_wait_run": compute_wait_run,
        "compute_get_logs": compute_get_logs,
        "compute_cancel_run": compute_cancel_run,
        "compute_list_runs": compute_list_runs,
        "compute_sync_project": compute_sync_project,
        "compute_push_project": compute_push_project,
        "compute_collect_results": compute_collect_results,
        "compute_fetch_results": compute_fetch_results,
        "compute_doctor": compute_doctor,
        "compute_scan_storage": compute_scan_storage,
        "compute_report_storage": compute_report_storage,
        "compute_check_storage": compute_check_storage,
        "compute_plan_storage_cleanup": compute_plan_storage_cleanup,
        "compute_plan_storage_adoption": compute_plan_storage_adoption,
        "compute_get_storage_plan": compute_get_storage_plan,
        "compute_reconcile_storage_plan": compute_reconcile_storage_plan,
        "compute_apply_storage_plan": compute_apply_storage_plan,
        "compute_pin_run": compute_pin_run,
    }
    missing = set(handlers) - set(TOOLS_BY_NAME)
    if missing:  # pragma: no cover - programming error
        raise RuntimeError(f"MCP handlers without a spec: {sorted(missing)}")
    for spec in TOOL_SPECS:
        register(spec, handlers[spec.name])
    return server


def main() -> None:  # pragma: no cover - entry point
    server = build_server()
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
