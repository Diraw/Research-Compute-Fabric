"""Project registry: where a project lives on each node and how to sync it.

RCF never assumes a shared filesystem for project data: every project declares a
path per node, and only the code checkout is ever touched by ``sync`` (never the
data, never an untracked working copy with local changes).
"""

from __future__ import annotations

from ..core.config import Config
from ..core.models import (
    Check,
    EnvironmentStep,
    NodeKind,
    NodeSpec,
    ProbeStatus,
    ProjectSpec,
    ProjectView,
    SyncPlan,
)
from ..core.nodeops import NodeOps
from ..core.runner import Runner
from ..security.policy import SecurityPolicy
from .push import ProjectPusher


class ProjectService:
    def __init__(self, config: Config, runner: Runner, *, node_ops: NodeOps | None = None) -> None:
        self.config = config
        self.runner = runner
        self.ops = node_ops or NodeOps(runner)
        self.pusher = ProjectPusher(config, runner, node_ops=self.ops)
        self.policy: SecurityPolicy = config.policy()

    def view(self, project: ProjectSpec, node: NodeSpec) -> ProjectView:
        path = project.path_for(node.id)
        checks: list[Check] = []
        if not path:
            return ProjectView(
                project_id=project.id,
                node_id=node.id,
                path=None,
                exists=ProbeStatus.FAIL,
                checks=[
                    Check(
                        name="path",
                        status=ProbeStatus.FAIL,
                        detail=f"no path configured for node {node.id!r} (add [projects.{project.id}.paths])",
                    )
                ],
            )
        state = self.ops.git_state(node, path)
        repo = state.get("rcf_repo", "")
        if repo == "missing":
            checks.append(Check(name="path", status=ProbeStatus.FAIL, detail=f"{path} does not exist"))
            return ProjectView(
                project_id=project.id, node_id=node.id, path=path, exists=ProbeStatus.FAIL, checks=checks
            )
        exists = ProbeStatus.OK
        checks.append(Check(name="path", status=ProbeStatus.OK, detail=path))
        if repo != "git":
            checks.append(
                Check(
                    name="git",
                    status=ProbeStatus.WARN,
                    detail="directory is not a git checkout; commit traceability unavailable",
                )
            )
        commit = state.get("rcf_commit") or None
        branch = state.get("rcf_branch") or None
        dirty_raw = state.get("rcf_dirty")
        dirty = None if dirty_raw is None else dirty_raw == "yes"
        if commit:
            checks.append(Check(name="commit", status=ProbeStatus.OK, detail=commit[:12]))
        if dirty:
            checks.append(
                Check(
                    name="worktree",
                    status=ProbeStatus.WARN,
                    detail="uncommitted changes present; RCF will not overwrite them",
                )
            )
        if project.commit and commit and not commit.startswith(project.commit):
            checks.append(
                Check(
                    name="expected_commit",
                    status=ProbeStatus.WARN,
                    detail=f"checked out {commit[:12]} but project pins {project.commit}",
                )
            )
        return ProjectView(
            project_id=project.id,
            node_id=node.id,
            path=path,
            exists=exists,
            git_commit=commit,
            git_branch=branch,
            git_dirty=dirty,
            checks=checks,
        )

    def push_plan(
        self,
        project: ProjectSpec,
        target: NodeSpec,
        *,
        source: NodeSpec,
        force: bool = False,
        scope: str = "code",
    ):
        """What would be copied from ``source`` to ``target`` (no side effects)."""

        self.policy.check("project.push_plan")
        return self.pusher.plan(project, target, source=source, force=force, scope=scope)

    def push_apply(self, project: ProjectSpec, target: NodeSpec, *, source: NodeSpec, plan):
        self.policy.check("project.push")
        return self.pusher.apply(project, target, plan, source=source)

    def sync_plan(self, project: ProjectSpec, node: NodeSpec, *, view: ProjectView | None = None) -> SyncPlan:
        view = view or self.view(project, node)
        blockers: list[str] = []
        steps: list[EnvironmentStep] = []
        if view.exists is not ProbeStatus.OK:
            blockers.append(f"project path is not usable on node {node.id!r}: {view.path}")
            return SyncPlan(
                project_id=project.id,
                node_id=node.id,
                path=view.path,
                blockers=blockers,
                current_commit=view.git_commit,
            )
        if not project.git_url:
            blockers.append("project has no git_url; RCF cannot sync it")
        if view.git_dirty:
            blockers.append("working tree has uncommitted changes; refusing to switch commits")
        if not view.git_commit:
            blockers.append("directory is not a git checkout")
        target = project.commit or f"origin/{project.default_branch}"
        if view.exists is ProbeStatus.OK and view.git_commit and not blockers:
            fetch_argv = ["git", "-C", view.path or "", "fetch", "--prune", "origin"]
            checkout_argv = ["git", "-C", view.path or "", "checkout", target]
            steps = [
                EnvironmentStep(
                    description=f"fetch remotes in {view.path}",
                    command=fetch_argv,
                    requires_network=True,
                ),
                EnvironmentStep(
                    description=f"check out {target}",
                    command=checkout_argv,
                    requires_network=False,
                ),
            ]
        return SyncPlan(
            project_id=project.id,
            node_id=node.id,
            path=view.path,
            steps=steps,
            blockers=blockers,
            current_commit=view.git_commit,
            target_commit=target,
            requires_authorization=True,
        )

    def sync_apply(
        self, plan: SyncPlan, node: NodeSpec, *, confirmed: bool
    ) -> list[dict[str, object]]:
        self.policy.check("project.sync")
        self.policy.check_confirmation("project.sync", confirmed)
        if plan.blockers:
            from ..core.errors import PermissionDenied

            raise PermissionDenied("sync plan has blockers", detail={"blockers": plan.blockers})
        results: list[dict[str, object]] = []
        for step in plan.steps:
            argv = list(step.command)
            result = self._run_step(node, argv)
            results.append(
                {
                    "description": step.description,
                    "command": argv,
                    "returncode": result.returncode,
                    "output": (result.stdout + result.stderr)[-800:],
                }
            )
            if result.returncode != 0:
                break
        return results

    def _run_step(self, node: NodeSpec, argv: list[str]):
        if node.kind is NodeKind.LOCAL:
            return self.runner.run(argv, timeout=600)
        from ..core.runner import join_command
        from ..executors.ssh import SshTransport

        return SshTransport.from_node(node, self.runner).run(join_command(argv), timeout=600)
