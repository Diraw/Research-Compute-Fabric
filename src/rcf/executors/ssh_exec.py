"""SSH executor: no daemon on the remote host, state kept in plain files.

Everything it needs to know about a job lives in the remote run directory, so a
dropped SSH connection, a closed laptop or a restarted RCF process cannot lose
the job: the next ``status`` call reads the files (or the scheduler) again.
"""

from __future__ import annotations

import json
import posixpath
import time

from ..core.errors import ExecutorError
from ..core.ids import iso, now_utc, parse_iso
from ..core.models import (
    JobHandle,
    JobSpec,
    JobStatus,
    NodeSpec,
    RunState,
    SubmissionReceipt,
)
from ..core.runner import CommandResult, shquote
from ..progress.pilot import pilot_files, pilot_interpreter, pilot_timeout
from ..resources import parsers
from ..security.redaction import redact
from ..storage.output_probe import parse_output_manifest
from .base import BaseExecutor
from .scripts import (
    STATUS_PROBE,
    bootstrap_command,
    cancel_command,
    newer_files_command,
    run_script,
    tail_command,
)
from .ssh import SshTransport


class SshExecutor(BaseExecutor):
    kind = "ssh"

    def __init__(
        self,
        node: NodeSpec,
        transport: SshTransport,
        *,
        submit_timeout: float = 60.0,
        query_timeout: float = 45.0,
    ) -> None:
        super().__init__(node.id)
        self.node = node
        self.transport = transport
        self.submit_timeout = submit_timeout
        self.query_timeout = query_timeout

    def run_dir(self, run_id: str) -> str:
        return self.node.runs_root(run_id)

    # -- submit ------------------------------------------------------------- #
    def submit(self, spec: JobSpec) -> SubmissionReceipt:
        run_dir = spec.run_dir or self.run_dir(spec.run_id)
        timeout = pilot_timeout(spec.metadata)
        script = run_script(
            pilot_seconds=timeout,
            pilot_interpreter=pilot_interpreter(spec.command.argv),
            run_id=spec.run_id,
            run_dir=run_dir,
            command=spec.command,
            workdir=spec.command.workdir,
            created_at=iso(now_utc()) or "",
        )
        metadata = {
            "run_id": spec.run_id,
            "project_id": spec.project_id,
            "env_id": spec.env_id,
            "resources": spec.resources.model_dump(mode="json"),
            "command": spec.command.model_dump(mode="json"),
            "workdir": spec.command.workdir,
            "result_dir": spec.result_dir,
            "created_at": iso(now_utc()),
            "rcf_schema": 1,
            **spec.metadata,
        }
        remote = " && ".join(
            [
                self.transport.write_file_command(
                    posixpath.join(run_dir, "run.json"),
                    json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True),
                ),
                self.transport.write_file_command(posixpath.join(run_dir, "run.sh"), script),
                self.transport.write_file_command(
                    posixpath.join(run_dir, "params.json"),
                    json.dumps(spec.metadata.get("params", {}), indent=2, ensure_ascii=False, sort_keys=True),
                ),
                *[
                    self.transport.write_file_command(posixpath.join(run_dir, name), text)
                    for name, text in pilot_files(
                        spec.command, timeout,
                        calibration_pilot=spec.metadata.get("pilot_requested", True),
                    ).items()
                ],
                bootstrap_command(run_dir),
            ]
        )
        result = self.transport.run(remote, timeout=self.submit_timeout)
        if result.returncode != 0 or "rcf_bootstrap=ok" not in result.stdout:
            raise ExecutorError(
                f"failed to launch run on {self.node.id!r}: {result.first_error_line()}",
                detail={
                    "node_id": self.node.id,
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip()[:600],
                    "stdout": result.stdout.strip()[:300],
                },
            )
        handle = JobHandle(
            kind=self.kind,
            node_id=self.node_id,
            workdir=run_dir,
            log_path=posixpath.join(run_dir, "run.log"),
            extra={"ssh_alias": self.transport.alias, "project_dir": spec.command.workdir},
        )
        return SubmissionReceipt(handle=handle, state=RunState.PENDING, raw=result.stdout.strip())

    # -- observe ------------------------------------------------------------ #
    def _probe(self, handle: JobHandle) -> tuple[dict[str, str], CommandResult]:
        run_dir = handle.workdir or ""
        result = self.transport.run(STATUS_PROBE.format(run_dir=_q(run_dir)), timeout=self.query_timeout)
        if result.returncode == 255 and not result.stdout.strip():
            from ..core.runner import classify_ssh_failure

            raise classify_ssh_failure(result, alias=self.transport.alias)
        return parsers.parse_key_value_block(result.stdout), result

    def status(self, handle: JobHandle) -> JobStatus:
        data, probe_result = self._probe(handle)
        _probe_stdout = probe_result.stdout
        _probe_stderr = probe_result.stderr
        state_raw = (data.get("state") or "").upper()
        started_at = data.get("started_at") or None
        finished_at = data.get("finished_at") or None
        detail_bits = []
        if started_at:
            detail_bits.append(f"started {started_at}")
        if finished_at:
            detail_bits.append(f"finished {finished_at}")
        if data.get("log_bytes"):
            detail_bits.append(f"{data['log_bytes']} B log")

        if state_raw == "EXITED":
            try:
                code = int(data.get("exit_code") or "")
            except ValueError:
                code = None
            state = RunState.SUCCEEDED if code == 0 else RunState.FAILED
            return JobStatus(
                state=state,
                exit_code=code,
                detail="; ".join(detail_bits) or "exit_code file present",
                observed_at=now_utc(),
                finished_at=_parse_stamp(data.get("finished_at")),
            )
        if state_raw == "RUNNING":
            if data.get("pid_marker") == "mismatch":
                return JobStatus(
                    state=RunState.LOST,
                    detail=(
                        "pid file points at a different process (pid reuse); "
                        "the job most likely died without writing an exit code"
                    ),
                    observed_at=now_utc(),
                )
            return JobStatus(
                state=RunState.RUNNING,
                detail="; ".join(detail_bits) or f"pid {data.get('pid')} alive",
                observed_at=now_utc(),
            )
        if state_raw == "LOST":
            return JobStatus(
                state=RunState.LOST,
                detail="process gone and no exit_code file (killed, or node rebooted)",
                observed_at=now_utc(),
            )
        if state_raw == "MISSING":
            return JobStatus(
                state=RunState.UNKNOWN,
                detail=f"run directory not found on {self.node.id}: {handle.workdir}",
                observed_at=now_utc(),
            )
        if state_raw == "STARTING":
            return JobStatus(
                state=RunState.PENDING,
                detail="launcher finished; the script has not written its pid yet",
                observed_at=now_utc(),
            )
        # No state at all means the probe itself failed.  That is "cannot
        # observe", not "job is pending": raising lets the manager keep the last
        # known state and record an observe_failed event.
        raise ExecutorError(
            f"status probe on {self.node.id!r} returned no state",
            detail={
                "run_dir": handle.workdir,
                "stdout": redact(_probe_stdout)[:300],
                "stderr": redact(_probe_stderr)[:300],
            },
        )

    def logs(self, handle: JobHandle, *, tail: int = 200) -> str:
        run_dir = handle.workdir or ""
        result = self.transport.run(tail_command(run_dir, tail), timeout=self.query_timeout)
        if "RCF: run.log not found" in result.stdout and not self.exists(handle):
            return (
                f"RCF: the run directory {run_dir} no longer exists on {self.node.id!r}; "
                "its log and artifacts were removed outside RCF"
            )
        if result.returncode not in (0,) and not result.stdout.strip():
            raise ExecutorError(
                f"cannot read logs on {self.node.id!r}: {result.first_error_line()}"
            )
        return result.stdout

    def exists(self, handle: JobHandle) -> bool:
        """True when the run directory is still present on the node."""

        run_dir = shquote(handle.workdir or "")
        probe = self.transport.run(f"[ -d {run_dir} ] && echo yes || echo no", timeout=self.query_timeout)
        return "yes" in probe.stdout

    def cancel(self, handle: JobHandle) -> JobStatus:
        run_dir = handle.workdir or ""
        # The launcher returns before run.sh has written its pid, so a cancel
        # that lands in that window must wait briefly instead of reporting
        # "nothing to cancel" for a job that is about to start.
        deadline = time.monotonic() + 3.0
        while True:
            result = self.transport.run(cancel_command(run_dir), timeout=self.query_timeout + 30)
            data = parsers.parse_key_value_block(result.stdout)
            outcome = data.get("cancelled", "")
            if outcome != "no_pid" or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if outcome not in {"term", "force"} or result.returncode != 0:
            observed = self.status(handle)
            if observed.state in (RunState.SUCCEEDED, RunState.FAILED):
                return observed
            return JobStatus(
                state=RunState.UNKNOWN,
                detail=f"cancellation not confirmed ({outcome or 'no acknowledgement'})",
                observed_at=now_utc(),
            )
        observed = self.status(handle)
        if observed.state not in (RunState.LOST, RunState.SUCCEEDED, RunState.FAILED):
            return JobStatus(
                state=RunState.UNKNOWN,
                detail="process still alive after cancellation request",
                observed_at=now_utc(),
            )
        if observed.state in (RunState.SUCCEEDED, RunState.FAILED):
            return observed
        return JobStatus(
            state=RunState.CANCELLED,
            detail=f"cancelled ({outcome})",
            observed_at=now_utc(),
        )

    def workdir_changes(
        self, handle: JobHandle, *, limit: int = 50, root: str | None = None
    ) -> list[dict[str, object]]:
        """Files in the project checkout modified after the run started.

        A diagnostic, not a result contract: scientific scripts often write
        their artifacts next to their sources instead of into the output dir.
        """

        project_root = root or str((handle.extra or {}).get("project_dir") or "")
        if not project_root:
            return []
        reference = posixpath.join(handle.workdir or "", "started_at")
        result = self.transport.run(
            newer_files_command(root=project_root, reference=reference, limit=limit),
            timeout=self.query_timeout,
        )
        return parse_output_manifest(result.stdout, returncode=result.returncode)

    def result_manifest(
        self, handle: JobHandle, *, limit: int = 200, root: str | None = None,
        hashes: bool = False,
    ) -> list[dict[str, object]]:
        from .scripts import manifest_command

        result_dir = root or posixpath.join(handle.workdir or "", "outputs")
        result = self.transport.run(
            manifest_command(result_dir, limit=limit, hashes=hashes), timeout=self.query_timeout,
        )
        return parse_output_manifest(result.stdout, returncode=result.returncode)


def _parse_stamp(value: str | None):
    """Parse the ISO-8601 stamp written by the run script; never raise."""

    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def _q(value: str) -> str:
    from ..core.runner import shquote

    return shquote(value)
