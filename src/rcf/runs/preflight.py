"""Recheck recorded plan evidence before claiming a new submission.

This is drift detection, not strict immutability: the checkout, environment and
inputs remain mutable, including after this check. Git's dirty flag cannot
identify further edits to an already-dirty tree; input manifests hash file
names/roots/sizes, not file contents. No extra content hashing is introduced.
"""

from __future__ import annotations

from typing import Any

from ..core.config import Config
from ..core.errors import Conflict, RcfError
from ..core.models import EnvironmentState, ProbeStatus, RunPlan
from .planner import RunPlanner


def _conflict(plan: RunPlan, evidence: str, message: str, **detail: Any) -> Conflict:
    return Conflict(
        f"submission preflight: {message}; create a new plan before submitting",
        hint="Run `rcf run plan` again, review the new plan, then submit its plan_id.",
        detail={
            "plan_id": plan.plan_id,
            "node_id": plan.node_id,
            "project_id": plan.request.project_id,
            "evidence": evidence,
            **detail,
        },
    )


def validate_plan(config: Config, planner: RunPlanner, plan: RunPlan) -> None:
    """Fail closed when recorded git/environment/input evidence cannot be matched.

    Call only for a *new* submission, before its atomic claim and dispatch;
    idempotent replay should return the existing record without rechecking.
    Probes reuse the planner's node-aware services. Nothing is installed,
    verified by a user command, synchronised, reserved or persisted here.
    Evidence absent from the original plan does not trigger new probes.
    """
    evidence = "configuration"
    try:
        node = config.node(plan.node_id)
        project = config.project(plan.request.project_id)
        path = plan.project_path or plan.workdir
        if plan.git_commit or plan.inputs is not None:
            evidence = "project_path"
            current_path = project.path_for(node.id)
            if not path or current_path != path:
                raise _conflict(
                    plan, evidence, "project checkout path changed or is unavailable",
                    expected=path, current=current_path,
                )

        if plan.git_commit:
            evidence = "git"
            view = planner.projects.view(project, node)
            if (
                view.exists is not ProbeStatus.OK
                or not view.git_commit
                or view.git_commit == "unknown"
                or view.git_commit != plan.git_commit
                or (
                    plan.git_dirty is not None
                    and (view.git_dirty is None or view.git_dirty != plan.git_dirty)
                )
            ):
                raise _conflict(
                    plan, evidence, "git commit/dirty evidence changed or cannot be rechecked",
                    expected={"commit": plan.git_commit, "dirty": plan.git_dirty},
                    current={"commit": view.git_commit, "dirty": view.git_dirty, "exists": view.exists.value},
                )

        if plan.env_id and plan.env_fingerprint:
            evidence = "environment"
            env = config.environment(plan.env_id)
            view = planner.environments.inspect(env, node, verify=False)
            if (
                view.state not in (EnvironmentState.COMPATIBLE, EnvironmentState.VERIFIED)
                or not view.fingerprint
                or view.fingerprint != plan.env_fingerprint
            ):
                raise _conflict(
                    plan, evidence, "environment fingerprint changed or cannot be rechecked",
                    env_id=plan.env_id, expected=plan.env_fingerprint,
                    current=view.fingerprint, state=view.state,
                )

        if plan.inputs is not None:
            evidence = "inputs"
            check, manifest = planner._inputs_check(project, node, path)
            if check is None or check.status is not ProbeStatus.OK or manifest is None:
                raise _conflict(
                    plan, evidence, "declared inputs cannot be rechecked",
                    expected=plan.inputs.digest,
                    current=manifest.digest if manifest else None,
                    reason=check.detail if check else "no input declarations or successful probe",
                )
            if manifest.digest != plan.inputs.digest:
                raise _conflict(
                    plan, evidence, "declared input listing digest changed",
                    expected=plan.inputs.digest, current=manifest.digest,
                )
    except Conflict as exc:
        # Keep our own structured conflicts, but annotate conflicts from services.
        if exc.detail.get("plan_id") == plan.plan_id and exc.detail.get("evidence") == evidence:
            raise
        raise _conflict(plan, evidence, "recorded evidence could not be rechecked", cause=exc.to_dict()) from exc
    except Exception as exc:
        cause = exc.to_dict() if isinstance(exc, RcfError) else {"type": type(exc).__name__, "message": str(exc)[:300]}
        raise _conflict(plan, evidence, "recorded evidence could not be rechecked", cause=cause) from exc
