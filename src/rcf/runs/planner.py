"""Run planning: turn an intent ("run this project on a GPU node") into a
reviewable, reproducible command.

A plan is deliberately boring: every decision is spelled out as a ``Check``
(ok / warn / fail) plus the exact argv, environment and working directory.  The
Agent reviews the plan; only then is anything submitted.  This is the boundary
between "thinking about it" and "doing it".
"""

from __future__ import annotations

import os
import posixpath
import shlex
from pathlib import Path

from ..core.config import Config
from ..core.errors import NotFound, SpecError
from ..core.ids import new_id, now_utc, stable_hash
from ..core.models import (
    RESERVED_RUN_PARAMETERS,
    Check,
    EnvironmentState,
    EnvironmentView,
    GpuSchedulabilityStatus,
    InputManifest,
    JobSpec,
    NodeKind,
    NodeSpec,
    OutputContract,
    PlannedCommand,
    ProbeStatus,
    ProjectSpec,
    ResourceRequirements,
    ResourceSnapshot,
    RunPlan,
    RunRequest,
)
from ..core.nodeops import NodeOps
from ..core.runner import shquote
from ..environments.manager import EnvironmentManager
from ..projects.service import ProjectService
from ..resources.inspector import ResourceInspector, gpu_key
from ..runs.store import Store
from ..security.policy import assert_safe_command


def _render_token(token: str, values: dict[str, str], *, where: str) -> str:
    try:
        return token.format_map(values)
    except KeyError as exc:
        raise SpecError(
            f"{where} references unknown placeholder {{{exc.args[0]}}}",
            hint=f"available: {', '.join(sorted(values))}",
            detail={"template": token},
        ) from None
    except (IndexError, ValueError) as exc:
        raise SpecError(f"{where} is not a valid template: {exc}", detail={"template": token}) from exc


def build_command(
    *,
    template: str,
    values: dict[str, str],
    workdir: str,
    env: dict[str, str],
    preamble: list[str],
    where: str,
) -> PlannedCommand:
    """Render a command template into argv, preserving argument boundaries.

    The template is split *before* substitution so a parameter that contains a
    space stays a single argument instead of silently becoming two.
    """

    shell_features = any(token in template for token in ("|", "&&", "||", ";", ">", "<", "$(", "`"))
    if shell_features:
        rendered = _render_token(template, values, where=where)
        return PlannedCommand(
            argv=[rendered],
            workdir=workdir,
            env=env,
            preamble=preamble,
            shell=True,
            preview="",
        )
    tokens = shlex.split(template, posix=True)
    if not tokens:
        raise SpecError(f"{where} renders to an empty command", detail={"template": template})
    argv = [_render_token(token, values, where=where) for token in tokens]
    return PlannedCommand(argv=argv, workdir=workdir, env=env, preamble=preamble, shell=False, preview="")


def command_preview(command: PlannedCommand) -> str:
    lines: list[str] = []
    if command.preamble:
        lines.append("# setup")
        lines.extend(command.preamble)
    if command.env:
        lines.append("# environment")
        lines.extend(f"export {k}={shquote(v)}" for k, v in sorted(command.env.items()))
    lines.append("# command")
    if command.shell:
        lines.append(command.argv[0])
    else:
        lines.append(" ".join(shquote(a) for a in command.argv))
    lines.append("# workdir")
    lines.append(command.workdir)
    return "\n".join(lines)


#: Prefix that makes an input entry resolve under ``data_paths`` instead of the
#: checkout.  Spelled as a prefix rather than a separate list so one declaration
#: can describe everything a run reads.
DATA_PREFIX = "data:"


class RunPlanner:
    def __init__(
        self,
        config: Config,
        store: Store,
        inspector: ResourceInspector,
        environments: EnvironmentManager,
        projects: ProjectService,
        *,
        node_ops: NodeOps | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.inspector = inspector
        self.environments = environments
        self.projects = projects
        self.ops = node_ops or NodeOps(inspector.runner)

    # -- entry point -------------------------------------------------------- #
    def plan(self, request: RunRequest) -> RunPlan:
        project = self.config.project(request.project_id)

        candidates = self._candidates(project, request)
        if not candidates:
            raise NotFound(
                f"no node can host project {project.id!r}",
                hint="declare [projects.<id>.paths] for at least one node, or pass node_id",
                detail={"project_id": project.id, "node_id": request.node_id},
            )

        attempts: list[tuple[NodeSpec, list[Check], ResourceSnapshot | None]] = []
        for node in candidates[:4]:
            env_id = self._resolve_env_id(project, node, request.env_id)
            try:
                snapshot = self.inspector.inspect(node)
            except Exception as exc:  # unreachable node is a *candidate* issue, not a hard failure
                checks = [Check(name="probe", status=ProbeStatus.FAIL, detail=str(exc)[:400])]
                attempts.append((node, checks, None))
                continue
            # The environment depends on the node, so resolve it per candidate.
            candidate_env = self.config.environment(env_id) if env_id else None
            checks = self._node_checks(node, project, candidate_env, request, snapshot)
            attempts.append((node, checks, snapshot))
            if not [c for c in checks if c.status is ProbeStatus.FAIL]:
                break

        node, checks, snapshot = attempts[-1]
        env_id = self._resolve_env_id(project, node, request.env_id)
        env_spec = self.config.environment(env_id) if env_id else None
        env_view = None
        if env_spec is not None and snapshot is not None:
            env_view = self.environments.inspect(env_spec, node)

        # Inputs are resolved for the node that actually won, and only when the
        # checkout itself is usable: otherwise the failure is already reported.
        inputs_manifest: InputManifest | None = None
        project_path = project.path_for(node.id) or ""
        path_ok = not any(c.name == "project_path" and c.status is ProbeStatus.FAIL for c in checks)
        if project_path and path_ok:
            inputs_check, inputs_manifest = self._inputs_check(project, node, project_path)
            if inputs_check is not None:
                checks.append(inputs_check)
                if inputs_check.status is ProbeStatus.FAIL:
                    inputs_manifest = None

        plan = self._assemble(
            request=request,
            project=project,
            node=node,
            snapshot=snapshot,
            checks=checks,
            env_view=env_view,
            env_id=env_id,
        )
        plan.inputs = inputs_manifest
        from ..progress.planning import attach_contract

        attach_contract(self.config, self.store, plan)
        self.store.save_plan(plan)
        return plan

    # -- candidate nodes ---------------------------------------------------- #
    def _candidates(self, project: ProjectSpec, request: RunRequest) -> list[NodeSpec]:
        if request.node_id:
            return [self.config.node(request.node_id)]
        nodes = []
        for node in self.config.nodes.values():
            if not project.path_for(node.id):
                continue
            if project.required_labels and not all(
                node.labels.get(k) == v for k, v in project.required_labels.items()
            ):
                continue
            nodes.append(node)
        nodes.sort(key=lambda n: (n.kind is NodeKind.SLURM, n.id))
        return nodes

    # -- checks ------------------------------------------------------------- #
    def _node_checks(
        self,
        node: NodeSpec,
        project: ProjectSpec,
        env_spec,
        request: RunRequest,
        snapshot: ResourceSnapshot,
    ) -> list[Check]:
        checks: list[Check] = []
        for check in snapshot.checks:
            if check.status is ProbeStatus.FAIL:
                checks.append(check)

        path = project.path_for(node.id)
        if not path:
            checks.append(
                Check(
                    name="project_path",
                    status=ProbeStatus.FAIL,
                    detail=f"project {project.id!r} has no path for node {node.id!r}",
                )
            )
        else:
            exists = self.ops.dir_exists(node, path)
            # Only OK lets the plan proceed: "unknown" (probe failed) is not a
            # licence to submit a job that will immediately fail on `cd`.
            checks.append(
                Check(
                    name="project_path",
                    status=ProbeStatus.OK if exists is ProbeStatus.OK else ProbeStatus.FAIL,
                    detail=f"{path}" if exists is ProbeStatus.OK else f"{path} not usable ({exists.value})",
                )
            )
            if exists is ProbeStatus.OK:
                state = self.ops.git_state(node, path)
                if state.get("rcf_repo") == "git":
                    commit = state.get("rcf_commit") or "unknown"
                    checks.append(
                        Check(
                            name="git_commit",
                            status=ProbeStatus.OK,
                            detail=commit[:12],
                            data={"commit": commit},
                        )
                    )
                    if state.get("rcf_dirty") == "yes":
                        checks.append(
                            Check(
                                name="git_dirty",
                                status=ProbeStatus.WARN,
                                detail="working tree has uncommitted changes; the run may not be reproducible",
                                data={"dirty": True},
                            )
                        )
                    if project.commit and not commit.startswith(project.commit):
                        checks.append(
                            Check(
                                name="pinned_commit",
                                status=ProbeStatus.WARN,
                                detail=f"checked out {commit[:12]} but project pins {project.commit}",
                            )
                        )
                else:
                    checks.append(
                        Check(
                            name="git_commit",
                            status=ProbeStatus.WARN,
                            detail="not a git checkout; code version cannot be recorded",
                        )
                    )

        req = request.requirements
        checks.extend(
            self.inspector.resource_checks(
                node,
                snapshot,
                cpus=req.cpus,
                memory_gb=req.memory_gb,
                gpus=req.gpus,
                gpu_memory_gb=req.gpu_memory_gb,
            )
        )

        if req.tasks > 1:
            if req.cpus and req.cpus < req.tasks:
                checks.append(
                    Check(
                        name="tasks",
                        status=ProbeStatus.FAIL,
                        detail=f"{req.tasks} ranks cannot fit in {req.cpus} CPUs",
                        data={"tasks": req.tasks, "cpus": req.cpus},
                    )
                )
            elif node.kind is NodeKind.SLURM:
                checks.append(
                    Check(
                        name="tasks",
                        status=ProbeStatus.OK,
                        detail=f"{req.tasks} ranks via srun on {partition_hint(node, request)}",
                    )
                )
            else:
                mpirun = self.ops.which(node, "mpirun")
                checks.append(
                    Check(
                        name="tasks",
                        status=ProbeStatus.OK if mpirun else ProbeStatus.FAIL,
                        detail=f"{req.tasks} ranks via mpirun ({mpirun})"
                        if mpirun
                        else "mpirun not found on this node; declare tasks=1 or install an MPI runtime",
                    )
                )

        assessments = self.inspector.assess_gpus(node, snapshot, leases=self.store.active_leases(node.id))
        selection, gpu_blockers = self.inspector.select_gpus(
            node,
            snapshot,
            assessments,
            req.gpus,
            required_vram_mb=req.gpu_memory_gb * 1024 if req.gpu_memory_gb else None,
            allow_shared=req.allow_shared_gpu,
        )
        if gpu_blockers:
            checks.append(
                Check(
                    name="gpu_availability",
                    status=ProbeStatus.FAIL,
                    detail="; ".join(gpu_blockers)[:600],
                    data={"assessment": [a.model_dump(mode="json") for a in assessments]},
                )
            )
        elif req.gpus:
            keys = ", ".join(gpu_key(snapshot.gpus[a.gpu_index]) if a.gpu_index < len(snapshot.gpus) else f"index:{a.gpu_index}" for a in selection)
            checks.append(
                Check(
                    name="gpu_availability",
                    status=ProbeStatus.OK if node.kind is not NodeKind.SLURM else ProbeStatus.WARN,
                    detail=f"selected {keys}" if node.kind is not NodeKind.SLURM else "delegated to the scheduler",
                )
            )

        if node.kind is NodeKind.SLURM and node.slurm:
            partition = self._effective_partition(node, req, snapshot)
            if not partition:
                checks.append(
                    Check(
                        name="partition",
                        status=ProbeStatus.FAIL,
                        detail="no partition requested and no slurm.default_partition configured",
                    )
                )
            elif node.slurm.partitions and partition not in node.slurm.partitions:
                checks.append(
                    Check(
                        name="partition",
                        status=ProbeStatus.FAIL,
                        detail=f"partition {partition!r} is not in the declared list {node.slurm.partitions}",
                    )
                )
            elif snapshot.scheduler.available and snapshot.scheduler.partitions:
                known = {p.name for p in snapshot.scheduler.partitions}
                checks.append(
                    Check(
                        name="partition",
                        status=ProbeStatus.OK if partition in known else ProbeStatus.WARN,
                        detail=f"{partition} {'found' if partition in known else 'not reported by sinfo'}",
                    )
                )

        if env_spec is not None:
            view = self.environments.inspect(env_spec, node)
            checks.append(
                Check(
                    name="environment",
                    status=ProbeStatus.OK
                    if view.state in (EnvironmentState.COMPATIBLE, EnvironmentState.VERIFIED)
                    else ProbeStatus.FAIL,
                    detail=f"{env_spec.id}: {view.state.value}"
                    + (f" (missing {', '.join(view.missing_packages)})" if view.missing_packages else ""),
                )
            )
        return checks

    def _inputs_check(
        self, project: ProjectSpec, node: NodeSpec, path: str
    ) -> tuple[Check | None, InputManifest | None]:
        """Resolve declared inputs on the node and turn missing data into a blocker.

        Inputs are written with an explicit root: ``data:`` means the file lives
        under ``data_paths`` for this node, anything else is relative to the
        checkout.  The two are kept apart because they travel by different
        routes -- code by git/push, data by its own copy -- and a run that reads
        a file outside its checkout must say so.

        Resolving them at plan time means "the data is not synced yet" is caught
        before a job is queued, and the exact file list is stored with the run,
        so a result can be traced back to the data that produced it.
        """

        if not project.inputs:
            return None, None

        code_patterns = [p for p in project.inputs if not p.startswith(DATA_PREFIX)]
        data_patterns = [p[len(DATA_PREFIX) :] for p in project.inputs if p.startswith(DATA_PREFIX)]

        data_root = project.data_path_for(node.id)
        if data_patterns and not data_root:
            return (
                Check(
                    name="project_inputs",
                    status=ProbeStatus.FAIL,
                    detail=(
                        f"inputs reference data files, but project {project.id!r} declares no "
                        f"data_paths for node {node.id!r}"
                    ),
                    data={"declared": list(project.inputs), "node": node.id},
                ),
                None,
            )

        try:
            code_entries = self.ops.resolve_inputs(node, path, code_patterns) if code_patterns else {}
            data_entries = (
                self.ops.resolve_inputs(node, data_root, data_patterns)
                if data_patterns and data_root
                else {}
            )
        except Exception as exc:  # a probe failure must not look like "no data"
            return (
                Check(
                    name="project_inputs",
                    status=ProbeStatus.FAIL,
                    detail=f"could not resolve inputs on {node.id}: {str(exc)[:300]}",
                ),
                None,
            )

        manifest = InputManifest.build(code_entries, data_entries)
        missing = [
            f"{prefix}{pattern}"
            for prefix, entries in (("", code_entries), (DATA_PREFIX, data_entries))
            for pattern, matches in entries.items()
            if not matches
        ]
        if missing:
            roots = ", ".join(
                part
                for part in (
                    f"checkout {path}" if code_entries else "",
                    f"data {data_root}" if data_entries else "",
                )
                if part
            )
            return (
                Check(
                    name="project_inputs",
                    status=ProbeStatus.FAIL,
                    detail="no file matches " + ", ".join(repr(m) for m in missing) + f" under {roots}",
                    data={"missing": missing, "declared": list(project.inputs)},
                ),
                manifest,
            )
        data_note = (
            f" ({len(manifest.data_files)} under data_paths)" if manifest.data_files else ""
        )
        return (
            Check(
                name="project_inputs",
                status=ProbeStatus.OK,
                detail=(
                    f"{len(manifest.files)} files, {manifest.total_bytes} bytes, "
                    f"sha256 {manifest.digest[:12]}{data_note}"
                ),
                data={
                    "digest": manifest.digest,
                    "files": len(manifest.files),
                    "data_files": len(manifest.data_files),
                },
            ),
            manifest,
        )

    # -- plan assembly ------------------------------------------------------ #
    def _assemble(
        self,
        *,
        request: RunRequest,
        project: ProjectSpec,
        node: NodeSpec,
        snapshot: ResourceSnapshot | None,
        checks: list[Check],
        env_view: EnvironmentView | None,
        env_id: str | None,
    ) -> RunPlan:
        reserved = RESERVED_RUN_PARAMETERS.intersection(request.params)
        if reserved:
            raise SpecError(f"run parameters use reserved names: {', '.join(sorted(reserved))}")
        run_id = new_id("run")
        run_dir = self._run_dir(node, run_id, snapshot)
        result_dir = self._result_dir(node, run_dir, project, run_id)
        project_path = project.path_for(node.id) or ""

        env_id = self._resolve_env_id(project, node, request.env_id)
        values = {
            "run_id": run_id,
            "project_id": project.id,
            "node_id": node.id,
            "script": project.script or "",
            "python": self._python_token(node, env_id),
            "output_dir": result_dir,
            "params_file": (
                str(Path(run_dir) / "params.json")
                if node.kind is NodeKind.LOCAL
                else posixpath.join(run_dir, "params.json")
            ),
            **{key: _stringify(value) for key, value in request.params.items()},
        }
        template = request.command_override or project.entrypoint
        # The launcher is spliced as *text* before the template is split: it
        # expands to several argv elements ("srun --exact -n 4 -c 1"), which a
        # per-token substitution would collapse into one quoted argument.
        env_spec = self.config.environment(env_id) if env_id else None
        template = template.replace(
            "{launcher}", self._launcher_token(node, request.requirements, env_spec)
        )
        preamble = (
            list(self.environments.preamble(self.config.environment(env_id), node)) if env_id else []
        )
        env_vars = {
            "RCF_RUN_ID": run_id,
            "RCF_PROJECT_ID": project.id,
            "RCF_NODE_ID": node.id,
            "RCF_OUTPUT_DIR": result_dir,
            "PYTHONUNBUFFERED": "1",
        }
        if env_view is not None and env_view.path:
            env_vars["RCF_ENV_ID"] = env_view.env_id
            env_vars["VIRTUAL_ENV"] = env_view.path
            if node.kind is NodeKind.LOCAL:
                env_vars["PATH"] = f"{env_view.path.rstrip('/')}/bin{os.pathsep}{os.environ.get('PATH', '')}"
            else:
                preamble.append(f"export PATH={shquote(env_view.path.rstrip('/') + '/bin')}:$PATH")
        if env_spec is not None:
            env_vars.update({k: v for k, v in env_spec.env_vars.items()})
        if project.progress is not None:
            from ..progress.planning import resolved_total

            try:
                total = resolved_total(project, request)
            except ValueError:
                total = None
            env_vars.update({
                "RCF_PROGRESS_FILE": str(Path(run_dir) / "progress.jsonl")
                if node.kind is NodeKind.LOCAL else posixpath.join(run_dir, "progress.jsonl"),
                "RCF_PROGRESS_PILOT": "1" if request.pilot_seconds else "0",
                "RCF_PROGRESS_MODE": project.progress.mode,
            })
            if total is not None:
                env_vars["RCF_PROGRESS_TOTAL"] = str(total)

        req = request.requirements
        partition = self._effective_partition(node, req, snapshot)
        command = build_command(
            template=template,
            values=values,
            workdir=project_path,
            env=env_vars,
            preamble=preamble,
            where="project.entrypoint" if not request.command_override else "command_override",
        )
        if partition and req.gpus and not req.partition:
            checks.append(
                Check(
                    name="partition_choice",
                    status=ProbeStatus.OK,
                    detail=f"using {partition!r} (it reports GPUs; request did not name one)",
                )
            )
        command.preview = command_preview(command)
        assert_safe_command(
            command.preview if not command.shell else " ".join(command.argv), context="run plan"
        )
        checks.append(self._output_contract_check(project, template, request))

        warnings: list[str] = []
        gpu_leases: list[str] = []
        if req.gpus:
            if node.kind is NodeKind.SLURM:
                warnings.append(
                    "GPU allocation is decided by Slurm; RCF records the request but does not reserve devices"
                )
            elif snapshot is not None:
                assessments = self.inspector.assess_gpus(
                    node, snapshot, leases=self.store.active_leases(node.id)
                )
                selection, blockers = self.inspector.select_gpus(
                    node,
                    snapshot,
                    assessments,
                    req.gpus,
                    required_vram_mb=req.gpu_memory_gb * 1024 if req.gpu_memory_gb else None,
                    allow_shared=req.allow_shared_gpu,
                )
                if not blockers:
                    shared = [a for a in selection if a.status is not GpuSchedulabilityStatus.AVAILABLE]
                    if shared:
                        warnings.append(
                            "sharing GPU(s) that already have activity: "
                            + "; ".join(f"gpu{a.gpu_index} {a.status.value}" for a in shared)
                        )
                    gpu_leases = [
                        gpu_key(snapshot.gpus[a.gpu_index]) if a.gpu_index < len(snapshot.gpus) else f"index:{a.gpu_index}"
                        for a in selection
                    ]
                    command.env["CUDA_VISIBLE_DEVICES"] = ",".join(
                        str(a.gpu_index) for a in selection
                    )
                    command.env["RCF_GPU_KEYS"] = ",".join(gpu_leases)

        if command.shell:
            warnings.append(
                "command uses shell syntax: argument quoting is the project's responsibility"
            )
        if node.kind is NodeKind.LOCAL:
            warnings.append(
                "local node: the job runs on the machine hosting RCF and is not sandboxed"
            )

        fingerprint = stable_hash(
            {
                "project": project.id,
                "node": node.id,
                "command": command.model_dump(mode="json"),
                "params": request.params,
                "env": env_view.fingerprint if env_view else None,
                "driver": (
                    snapshot.gpus[0].driver_version
                    if snapshot is not None and snapshot.gpus
                    else None
                ),
                "requirements": req.model_dump(mode="json"),
            },
            length=20,
        )
        plan = RunPlan(
            plan_id=new_id("plan"),
            run_id=run_id,
            created_at=now_utc(),
            request=request,
            node_id=node.id,
            node_kind=node.kind,
            executor=node.executor_kind,
            partition=partition,
            command=command,
            workdir=project_path,
            project_path=project_path or None,
            git_commit=next(
                (str(c.data.get("commit")) for c in checks if c.name == "git_commit" and c.data),
                None,
            ),
            git_dirty=any(c.name == "git_dirty" and c.data.get("dirty") for c in checks),
            run_dir=run_dir,
            result_dir=result_dir,
            env_id=env_id,
            env_fingerprint=env_view.fingerprint if env_view else None,
            resources=snapshot,
            checks=checks,
            warnings=warnings,
            gpu_leases=gpu_leases,
            requires_authorization=True,
            fingerprint=fingerprint,
        )
        return plan

    @staticmethod
    def _resolve_env_id(project: ProjectSpec, node: NodeSpec, requested: str | None) -> str | None:
        """Explicit request > per-node override > project default."""

        if requested:
            return requested
        return project.env_by_node.get(node.id) or project.env_id

    @staticmethod
    def _effective_partition(
        node: NodeSpec, req: ResourceRequirements, snapshot: ResourceSnapshot | None
    ) -> str | None:
        """Pick the partition a request will land in.

        A GPU request on a cluster whose default partition is CPU-only would
        otherwise queue forever or fail; if the scheduler reports a partition
        with GPU GRES, use it (unless the caller named one).
        """

        if node.kind is not NodeKind.SLURM or node.slurm is None:
            return None
        if req.partition:
            return req.partition
        default = node.slurm.default_partition
        if req.gpus <= 0 or snapshot is None:
            return default
        gpu_parts = [
            p for p in snapshot.scheduler.partitions if "gpu" in (p.gres or "").lower()
        ]
        if not gpu_parts:
            return default
        names = {p.name for p in gpu_parts}
        if default in names:
            return default
        declared = [p for p in node.slurm.partitions if p in names]
        return (declared or [p.name for p in gpu_parts])[0]

    @staticmethod
    def _output_contract_check(
        project: ProjectSpec, template: str, request: RunRequest
    ) -> Check:
        """Enforce the project's promise about where artifacts go.

        A run whose results cannot be found afterwards is a run you cannot
        reproduce, so the strict contract is the default: the entrypoint must
        hand ``{output_dir}`` to the script.  Projects that read
        ``$RCF_OUTPUT_DIR`` themselves declare ``output_contract = "env"``, and
        scripts that cannot be changed must opt out explicitly.
        """

        if request.command_override:
            return Check(
                name="output_contract",
                status=ProbeStatus.WARN,
                detail="command_override is used; RCF cannot verify where artifacts go",
            )
        if project.output_contract is OutputContract.ARGUMENT:
            if "{output_dir}" in template:
                return Check(
                    name="output_contract",
                    status=ProbeStatus.OK,
                    detail="entrypoint receives {output_dir}",
                )
            return Check(
                name="output_contract",
                status=ProbeStatus.FAIL,
                detail=(
                    "entrypoint must pass {output_dir} to the script; otherwise results "
                    "land in the checkout and cannot be collected reliably"
                ),
                data={"fix": "add --out {output_dir}, or set output_contract = \"env\"/\"none\""},
            )
        if project.output_contract is OutputContract.ENV:
            return Check(
                name="output_contract",
                status=ProbeStatus.OK,
                detail="script is declared to read $RCF_OUTPUT_DIR",
            )
        return Check(
            name="output_contract",
            status=ProbeStatus.WARN,
            detail=(
                "project opted out of the output contract; artifacts may be written "
                "outside the run directory (rcf run results will list what changed)"
            ),
        )

    def _launcher_token(self, node: NodeSpec, req, env_spec=None) -> str:
        """How to start ``tasks`` ranks on this node.

        ``srun`` is the right launcher only when the MPI in the environment speaks
        Slurm's PMI.  A conda MPICH built ``--with-pm=hydra:gforker`` does not: it
        starts four processes that each believe they are rank 0 of a one-rank
        world, with no error anywhere.  So when the environment ships ``mpiexec``,
        that is used instead -- it wires the ranks up itself.
        """

        if req.tasks <= 1:
            return ""
        if env_spec is not None and env_spec.mpi_launcher:
            return env_spec.mpi_launcher.format(tasks=req.tasks)
        if node.kind is NodeKind.SLURM:
            if env_spec is not None and self._env_has_command(env_spec, node, "mpiexec"):
                return f"mpiexec -n {req.tasks}"
            return f"srun --exact -n {req.tasks} -c 1"
        if env_spec is not None and self._env_has_command(env_spec, node, "mpirun"):
            return f"mpirun -np {req.tasks}"
        return f"mpiexec -n {req.tasks}"

    def _env_has_command(self, env_spec, node: NodeSpec, name: str) -> bool:
        try:
            return self.environments.has_command(env_spec, node, name)
        except Exception:  # probing must not turn into a plan failure
            return False

    def _python_token(self, node: NodeSpec, env_id: str | None) -> str:
        """What ``{python}`` expands to.

        With a declared environment this is the *exact* interpreter path, which
        is what makes venv projects work on every executor (the local executor
        does not run the shell preamble).  Without one, POSIX nodes get
        ``python3`` — Ubuntu 24.04+ has no bare ``python`` — and the local
        Windows node keeps ``python``.
        """

        env_spec = self.config.environment(env_id) if env_id else None
        if env_spec is not None:
            interpreter = self.environments.interpreter(env_spec, node)
            if interpreter and len(interpreter) == 1:
                return interpreter[0]
        if node.kind is not NodeKind.LOCAL or os.name != "nt":
            return "python3"
        return "python"

    def _run_dir(self, node: NodeSpec, run_id: str, snapshot: ResourceSnapshot | None) -> str:
        if node.kind is NodeKind.LOCAL:
            return str(Path(self.config.home) / "runs" / run_id)
        root = node.work_root
        if root.startswith("~") and snapshot is not None and snapshot.home_dir:
            # Resolve ``~`` now so every later path is absolute: shell variables
            # and env vars handed to the project must not rely on tilde expansion.
            root = posixpath.join(snapshot.home_dir, root[1:].lstrip("/"))
        return posixpath.join(root, "runs", run_id)

    def _result_dir(self, node: NodeSpec, run_dir: str, project: ProjectSpec, run_id: str) -> str:
        relative = (project.outputs or "outputs").format(
            run_id=run_id, project_id=project.id, node_id=node.id
        )
        if node.kind is NodeKind.LOCAL:
            return str(Path(run_dir) / relative)
        return posixpath.join(run_dir, relative)


def partition_hint(node: NodeSpec, request) -> str:
    return request.requirements.partition or (
        node.slurm.default_partition if node.slurm else "?"
    ) or "the default partition"


def _stringify(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def job_spec_from_plan(plan: RunPlan) -> JobSpec:
    return JobSpec(
        run_id=plan.run_id,
        run_dir=plan.run_dir or "",
        command=plan.command,
        log_path=(
            str(Path(plan.run_dir or "") / "run.log")
            if plan.node_kind is NodeKind.LOCAL
            else posixpath.join(plan.run_dir or "", "run.log")
        ),
        resources=plan.request.requirements.model_copy(update={"partition": plan.partition}),
        project_id=plan.request.project_id,
        env_id=plan.env_id,
        time_minutes=plan.request.requirements.time_minutes,
        result_dir=plan.result_dir,
        metadata={
            "plan_id": plan.plan_id,
            "params": plan.request.params,
            "env_fingerprint": plan.env_fingerprint,
            "git_commit": plan.git_commit,
            "git_dirty": plan.git_dirty,
            "gpu_leases": plan.gpu_leases,
            "pilot_seconds": plan.execution_timeout_seconds,
            "pilot_requested": plan.request.pilot_seconds is not None,
            "runtime_budget_seconds": plan.request.runtime_budget_seconds,
            "calibration_key": plan.calibration_key,
        },
    )
