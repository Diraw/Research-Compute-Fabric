"""Run Manager: plan → submit → observe → cancel, with persistent state.

Everything that must survive an Agent session lives in SQLite: the plan, the
job handle, the last observed state and a full event trail.  A dropped SSH
connection changes nothing except the timestamp of the next successful poll.
"""

from __future__ import annotations

from ..core.config import Config
from ..core.errors import (
    AuthFailed,
    CommandTimedOut,
    Conflict,
    ExecutableMissing,
    HostKeyRejected,
    HostUnreachable,
    NotFound,
    PermissionDenied,
    PlanExpired,
    SchedulerError,
    SubmissionUncertain,
    UnsupportedOperation,
)
from ..core.ids import now_utc
from ..core.models import (
    JobHandle,
    OutputContract,
    ResultManifest,
    RunEvent,
    RunPlan,
    RunRecord,
    RunRequest,
    RunState,
)
from ..core.runner import Runner
from ..executors.base import Executor
from ..executors.factory import build_executor
from ..security.policy import SecurityPolicy
from .planner import RunPlanner, job_spec_from_plan
from .preflight import validate_plan
from .store import Store


class RunManager:
    def __init__(
        self,
        config: Config,
        store: Store,
        planner: RunPlanner,
        runner: Runner,
    ) -> None:
        self.config = config
        self.store = store
        self.planner = planner
        self.runner = runner
        self.policy: SecurityPolicy = config.policy()

    # -- planning ----------------------------------------------------------- #
    def plan(self, request: RunRequest) -> RunPlan:
        self.policy.check("run.plan")
        return self.planner.plan(request)

    def get_plan(self, plan_id: str) -> RunPlan:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise NotFound(
                f"unknown plan {plan_id!r}",
                hint="plans are created by compute.plan_run / `rcf run plan`",
                detail={"plan_id": plan_id},
            )
        return plan

    def _assert_plan_fresh(self, plan: RunPlan) -> None:
        """Refuse a plan whose resource snapshot has aged out.

        The TTL exists because the plan promises something about the node.  A
        plan made an hour ago may describe a machine that is now busy, so the
        honest move is to look again rather than submit blind.
        """

        ttl = self.config.settings.runs.plan_ttl_minutes
        if ttl <= 0:
            return
        age_minutes = (now_utc() - plan.created_at).total_seconds() / 60
        if age_minutes <= ttl:
            return
        self.store.record_audit(
            actor="system",
            operation="run.submit",
            tier="execute",
            decision="denied",
            node_id=plan.node_id,
            run_id=plan.run_id,
            detail={"reason": "plan_expired", "age_minutes": round(age_minutes, 1), "ttl": ttl},
        )
        raise PlanExpired(
            f"plan {plan.plan_id} is {age_minutes:.0f} minutes old (limit {ttl}); "
            "its resource snapshot may no longer hold",
            hint="plan again to re-inspect the node: rcf run plan --project ... --node ...",
            detail={"plan_id": plan.plan_id, "age_minutes": round(age_minutes, 1), "ttl_minutes": ttl},
        )

    # -- submission --------------------------------------------------------- #
    def submit(
        self,
        plan_id: str,
        *,
        idempotency_key: str | None = None,
        confirmed: bool = False,
        actor: str = "cli",
    ) -> RunRecord:
        self.policy.check("run.submit")
        plan = self.get_plan(plan_id)

        if idempotency_key:
            existing = self.store.find_by_idempotency(idempotency_key)
            if existing is not None:
                prior_plan = self.store.get_plan(existing.plan_id) if existing.plan_id else None
                same_work = prior_plan is not None and prior_plan.fingerprint == plan.fingerprint
                if not same_work:
                    raise Conflict(
                        f"idempotency key {idempotency_key!r} was already used for different work",
                        hint="use a new key for a new request, or look up the existing run",
                        detail={
                            "idempotency_key": idempotency_key,
                            "existing_run": existing.run_id,
                            "existing_plan": existing.plan_id,
                        },
                    )
                return existing
        prior = self.store.get_run(plan.run_id)
        if prior is not None:
            return prior
        self._assert_plan_fresh(plan)

        if plan.blockers:
            self.store.record_audit(
                actor=actor,
                operation="run.submit",
                tier="execute",
                decision="denied",
                node_id=plan.node_id,
                run_id=plan.run_id,
                detail={"blockers": [c.model_dump(mode="json") for c in plan.blockers]},
            )
            raise PermissionDenied(
                "plan has blocking issues and cannot be submitted",
                hint="resolve them and plan again: " + "; ".join(c.name for c in plan.blockers),
                detail={"plan_id": plan.plan_id, "blockers": [c.model_dump(mode="json") for c in plan.blockers]},
            )
        self.policy.check_confirmation("run.submit", confirmed)
        from ..progress.planning import validate_submission

        validate_submission(self.config, self.store, plan)
        if plan.git_commit or plan.env_fingerprint or plan.inputs is not None:
            validate_plan(self.config, self.planner, plan)

        node = self.config.node(plan.node_id)
        requirements = plan.request.requirements
        leased = plan.gpu_leases if not requirements.allow_shared_gpu else []
        executor = build_executor(self.config, node, self.runner)
        # Store a directory-based recovery pointer before dispatch. A process
        # crash between launch and receipt must not erase the only evidence.
        recovery_handle = JobHandle(
            kind=plan.executor,
            node_id=plan.node_id,
            workdir=plan.run_dir,
            log_path=plan.run_dir + "/run.log" if plan.run_dir else None,
            extra={"project_dir": plan.project_path, "submission_uncertain": True},
        )
        record = RunRecord(
            run_id=plan.run_id,
            plan_id=plan.plan_id,
            project_id=plan.request.project_id,
            node_id=plan.node_id,
            executor=plan.executor,
            state=RunState.SUBMITTING,
            created_at=now_utc(),
            params=plan.request.params,
            command=plan.command,
            workdir=plan.project_path,
            log_path=plan.run_dir + "/run.log" if plan.run_dir else None,
            result_dir=plan.result_dir,
            env_id=plan.env_id,
            inputs=plan.inputs,
            env_fingerprint=plan.env_fingerprint,
            git_commit=plan.git_commit,
            git_dirty=plan.git_dirty,
            idempotency_key=idempotency_key,
            handle=recovery_handle,
            submitted_at=now_utc(),
        )
        from ..storage.ledger import Ledger
        from ..storage.roots import expand_root

        ledger = Ledger(self.store, nodes=self.config.nodes)
        roots = [expand_root(node, p, self.runner) for p in (plan.project_path, plan.run_dir, plan.result_dir) if p]
        # Short-lived coordination guard closes the cleanup-vs-submission race.
        # The persisted active run protects these roots after the claim commits.
        token = ledger.acquire_guard(node.id, roots or [str(self.config.home)], operation="run.submit", owner_id=plan.run_id)
        try:
            record, claimed = self.store.claim_submission(record, keys=leased)
        finally:
            ledger.release_guard(token)
        if not claimed:
            return record
        try:
            receipt = executor.submit(job_spec_from_plan(plan))
        except Exception as exc:
            # A transport failure happens *after* the remote side may have
            # started the job. Recording FAILED there would be a lie that leads
            # to duplicate submissions, so those cases become UNKNOWN.
            # Only an explicit rejection proves that nothing was launched.
            # Unknown exceptions can happen after the remote side accepted it.
            uncertain = not isinstance(exc, (SchedulerError, ExecutableMissing)) or isinstance(
                exc, (HostUnreachable, AuthFailed, HostKeyRejected, CommandTimedOut, SubmissionUncertain)
            )
            record.state = RunState.UNKNOWN if uncertain else RunState.FAILED
            record.error = (
                f"submission outcome unknown ({exc}); a job may exist on the node — "
                "inspect the run directory or the scheduler before retrying"
                if uncertain
                else str(exc)[:800]
            )
            record.finished_at = None if uncertain else now_utc()
            if not uncertain:
                record.handle = None
            if not self.store.update_run(record):
                record = self.get(record.run_id)
            self._event(record.run_id, "submit_failed", record.error or "")
            self.store.record_audit(
                actor=actor,
                operation="run.submit",
                tier="execute",
                decision="error",
                node_id=plan.node_id,
                run_id=plan.run_id,
                detail={"error": str(exc)[:400], "uncertain": uncertain},
            )
            raise

        record.handle = receipt.handle
        record.state = receipt.state
        record.log_path = receipt.handle.log_path
        record.last_observed_at = now_utc()
        if not self.store.update_run(record):
            return self.get(record.run_id)
        self._event(
            record.run_id,
            "submitted",
            f"executor={record.executor} node={record.node_id} job_id={receipt.handle.job_id}",
            {"raw": receipt.raw[:400], "handle": receipt.handle.model_dump(mode="json")},
        )
        self.store.record_audit(
            actor=actor,
            operation="run.submit",
            tier="execute",
            decision="allowed",
            node_id=plan.node_id,
            run_id=plan.run_id,
            detail={
                "plan_id": plan.plan_id,
                "executor": plan.executor,
                "job_id": receipt.handle.job_id,
                "cmd": plan.command.argv[:8],
                "gpu_leases": leased,
            },
        )
        return self.refresh(record.run_id)

    # -- observation -------------------------------------------------------- #
    def get(self, run_id: str) -> RunRecord:
        record = self.store.get_run(run_id)
        if record is None:
            raise NotFound(f"unknown run {run_id!r}", detail={"run_id": run_id})
        return record

    def refresh(self, run_id: str) -> RunRecord:
        self.policy.check("run.status")
        record = self.get(run_id)
        return self._refresh_record(record)

    def _refresh_record(self, record: RunRecord) -> RunRecord:
        if record.state.terminal or record.handle is None:
            return record
        node = self.config.node(record.node_id)
        executor = build_executor(self.config, node, self.runner)
        try:
            status = executor.status(record.handle)
        except Exception as exc:
            # A transient network failure must not lose the run: keep the last
            # known state and record why the observation failed.
            self._event(record.run_id, "observe_failed", str(exc)[:600])
            self.store.record_observation_error(record.run_id, f"observation failed: {exc}"[:600])
            return self.get(record.run_id)

        # A probe that succeeds makes an earlier failure history: the stored
        # error must not outlive it, otherwise a finished run keeps advertising
        # an observation failure that no longer describes anything.
        cleared_error = record.error is not None
        if cleared_error:
            record.error = None
            self._event(
                record.run_id,
                "observe_recovered",
                "observation recovered; previous error cleared",
            )

        uncertain = record.handle.extra.get("submission_uncertain", False)
        if uncertain and status.state is RunState.PENDING and not record.handle.job_id:
            # An empty directory only proves an upload, not an accepted launch.
            status.state = RunState.UNKNOWN
        recovered = uncertain and (
            status.state.terminal or status.state is RunState.RUNNING or record.handle.job_id
        )
        if recovered:
            record.handle.extra.pop("submission_uncertain", None)
        changed = (
            cleared_error
            or recovered
            or status.state is not record.state
            or status.exit_code != record.exit_code
        )
        record.state = status.state
        record.exit_code = status.exit_code if status.exit_code is not None else record.exit_code
        # The probe succeeded: this is when the saved state became known-true.
        # ``last_observed_at`` also moves on failed attempts, so staleness must
        # not be computed from it.
        record.state_observed_at = status.observed_at
        record.last_observed_at = status.observed_at
        if status.state.terminal and record.finished_at is None:
            record.finished_at = status.finished_at or status.observed_at
            self._verify_artifacts(record, executor)
            if status.state in (RunState.FAILED, RunState.LOST):
                self._diagnose_failure(record, executor)
            changed = True
        if changed:
            if not self.store.update_run(record):
                return self.get(record.run_id)
            self._event(
                record.run_id,
                "state",
                f"{status.state.value}: {status.detail}"[:500],
                {
                    "scheduler_state": status.scheduler_state,
                    "node_list": status.node_list,
                    "exit_code": status.exit_code,
                },
            )
        return record

    def list(self, *, limit: int = 50, state: RunState | None = None) -> list[RunRecord]:
        self.policy.check("run.list")
        return self.store.list_runs(limit=limit, state=state)

    def events(self, run_id: str, *, limit: int = 100) -> list[RunEvent]:
        self.get(run_id)
        return self.store.list_events(run_id, limit=limit)

    def logs(self, run_id: str, *, tail: int = 200) -> str:
        self.policy.check("run.logs")
        record = self.get(run_id)
        if record.handle is None:
            raise UnsupportedOperation(
                f"run {run_id!r} was never submitted; there are no logs",
                detail={"run_id": run_id, "state": record.state.value},
            )
        node = self.config.node(record.node_id)
        executor = build_executor(self.config, node, self.runner)
        return executor.logs(record.handle, tail=tail)

    # -- control ------------------------------------------------------------ #
    def cancel(self, run_id: str, *, confirmed: bool = False, actor: str = "cli") -> RunRecord:
        self.policy.check("run.cancel")
        self.policy.check_confirmation("run.cancel", confirmed)
        record = self.get(run_id)
        if record.state.terminal:
            return record
        if record.handle is None:
            raise Conflict(
                "cannot confirm cancellation of an unknown submission without a recovery handle",
                hint="inspect the run directory/scheduler before releasing resources or creating a new plan",
                detail={"run_id": run_id, "state": record.state.value},
            )
        if record.handle.extra.get("submission_uncertain"):
            record = self._refresh_record(record)
            if record.state.terminal:
                return record
            if record.state in (RunState.SUBMITTING, RunState.UNKNOWN):
                raise Conflict(
                    "cannot confirm cancellation: submission outcome is still unknown",
                    hint="recover the job from its run directory or scheduler; leases remain reserved",
                    detail={"run_id": run_id, "run_dir": record.handle.workdir},
                )
        node = self.config.node(record.node_id)
        executor: Executor = build_executor(self.config, node, self.runner)
        status = executor.cancel(record.handle)
        if not status.state.terminal:
            # Nothing could be confirmed (e.g. no pid file yet): do not pretend
            # the run is cancelled, and keep any GPU lease.
            record.error = f"cancel not confirmed: {status.detail}"[:600]
            record.last_observed_at = status.observed_at
            self.store.record_observation_error(run_id, record.error)
            self._event(run_id, "cancel_unconfirmed", status.detail)
            return self.get(run_id)
        record.state = status.state
        record.exit_code = status.exit_code if status.exit_code is not None else record.exit_code
        record.finished_at = now_utc()
        record.state_observed_at = status.observed_at
        record.last_observed_at = status.observed_at
        if not self.store.update_run(record):
            return self.get(run_id)
        self._event(run_id, "cancelled", status.detail, {"scheduler_state": status.scheduler_state})
        self.store.record_audit(
            actor=actor,
            operation="run.cancel",
            tier="execute",
            decision="allowed",
            node_id=record.node_id,
            run_id=run_id,
            detail={"detail": status.detail},
        )
        return record

    # -- results ------------------------------------------------------------ #
    def results(self, run_id: str, *, limit: int = 200) -> ResultManifest:
        self.policy.check("run.results")
        record = self.get(run_id)
        if record.handle is None:
            raise UnsupportedOperation(
                f"run {run_id!r} was never submitted", detail={"run_id": run_id}
            )
        node = self.config.node(record.node_id)
        executor = build_executor(self.config, node, self.runner)
        manifest_fn = getattr(executor, "result_manifest", None)
        entries = manifest_fn(record.handle, limit=limit, root=record.result_dir) if callable(manifest_fn) else []
        note = (
            "RCF lists results in place; it does not transfer bulk data between nodes "
            "(see docs/roadmap.md). Use the node's own tools for large transfers."
        )
        changes: list[dict] = []
        if not entries:
            # The script may have written its artifacts next to the sources
            # instead of into $RCF_OUTPUT_DIR. Report what actually changed in
            # the checkout, clearly labelled as a diagnostic.
            changes_fn = getattr(executor, "workdir_changes", None)
            if callable(changes_fn):
                try:
                    changes = changes_fn(record.handle, limit=50, root=record.workdir)
                except Exception:  # noqa: BLE001 - a diagnostic must never fail the call
                    changes = []
            if changes:
                note = (
                    f"The declared output directory is empty, but {len(changes)} file(s) changed "
                    "in the project directory during this run (listed above). RCF did not "
                    "create them and will not move them: have the script write to "
                    "$RCF_OUTPUT_DIR for a stable manifest. " + note
                )
        return ResultManifest(
            run_id=run_id,
            node_id=record.node_id,
            result_dir=record.result_dir,
            entries=entries,
            truncated=len(entries) >= limit,
            workdir_changes=changes,
            note=note,
        )

    # -- internals ---------------------------------------------------------- #
    #: Failure patterns that an Agent (or a human) should not have to guess.
    FAILURE_HINTS: tuple[tuple[str, str], ...] = (
        ("command not found", "the command is not on PATH in the job environment"),
        ("No module named", "a Python package is missing from this environment"),
        ("ModuleNotFoundError", "a Python package is missing from this environment"),
        ("Permission denied", "the job cannot read/write the path it needs"),
        ("cannot open shared object file", "a compiled library is missing from the environment"),
        ("Segmentation fault", "the program crashed (see the log for the failing step)"),
    )

    def _diagnose_failure(self, record: RunRecord, executor: Executor) -> None:
        """Turn 'exit code 127' into 'the command is not on PATH'."""

        exit_code = record.exit_code
        if exit_code == 137:
            self._event(
                record.run_id,
                "diagnosis",
                "exit 137: killed (out of memory, or walltime limit reached)",
                {"exit_code": exit_code},
            )
            return
        try:
            tail = executor.logs(record.handle, tail=40) if record.handle else ""
        except Exception:  # noqa: BLE001 - diagnosis must never fail the refresh
            tail = ""
        for pattern, hint in self.FAILURE_HINTS:
            if pattern.lower() in tail.lower():
                self._event(
                    record.run_id,
                    "diagnosis",
                    f"{pattern}: {hint}",
                    {"exit_code": exit_code, "pattern": pattern},
                )
                return
        if exit_code == 127:
            self._event(
                record.run_id,
                "diagnosis",
                "exit 127: interpreter or command not found in the job environment",
                {"exit_code": exit_code},
            )

    def _verify_artifacts(self, record: RunRecord, executor: Executor) -> None:
        """Close the loop on the artifact contract.

        The plan-time check only proves the entrypoint *mentions* the output
        directory. This is the part that cannot be faked: after a successful
        run, look at whether anything actually landed there.
        """

        if record.state is not RunState.SUCCEEDED or record.contract_violation:
            return
        if record.handle is None or not record.project_id:
            return
        try:
            contract = self.config.project(record.project_id).output_contract
        except NotFound:
            return
        if contract is OutputContract.NONE:
            # Declared as unmanaged: an empty output dir is expected, not a fault.
            # Record it once so a reader can tell "nothing to check" from "checked empty".
            self._event(
                record.run_id,
                "output_contract_unmanaged",
                "project declares output_contract=none; artifact location was not verified",
            )
            return
        plan = self.store.get_plan(record.plan_id) if record.plan_id else None
        if plan is not None and plan.request.command_override:
            # The plan already warned that an override hides where artifacts go.
            # Marking a violation here would contradict that warning and punish a
            # legitimate probe command for not writing into the project's outputs.
            self._event(
                record.run_id,
                "output_contract_unchecked",
                "command_override was used; RCF cannot verify where artifacts go",
                {"result_dir": record.result_dir},
            )
            return
        manifest_fn = getattr(executor, "result_manifest", None)
        if not callable(manifest_fn):
            return
        try:
            entries = manifest_fn(record.handle, limit=1, root=record.result_dir)
        except Exception:  # noqa: BLE001 - never fail a finished run on a check
            return
        if entries:
            return
        record.contract_violation = True
        self._event(
            record.run_id,
            "output_contract_violation",
            f"run succeeded but {record.result_dir} is empty (contract={contract.value})",
            {"result_dir": record.result_dir, "contract": contract.value},
        )

    def _event(self, run_id: str, kind: str, message: str = "", data: dict | None = None) -> None:
        self.store.add_event(
            RunEvent(run_id=run_id, at=now_utc(), kind=kind, message=message, data=data or {})
        )
