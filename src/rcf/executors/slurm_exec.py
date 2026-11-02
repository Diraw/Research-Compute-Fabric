"""Slurm executor.

Rules enforced here:

* RCF never bypasses the scheduler.  Work is submitted with ``sbatch`` from the
  login node and never launched directly on a compute node over SSH.
* The generated script writes the same ``exit_code`` / ``pid`` files as the SSH
  executor, so a Run can be reconciled even when accounting (``sacct``) is
  unavailable or lagging.
* ``sbatch --parsable`` is used to capture the job id deterministically, with a
  fallback to parsing ``Submitted batch job N`` for older Slurm builds.
"""

from __future__ import annotations

import json
import posixpath
import re

from ..core.errors import SchedulerError, SubmissionUncertain
from ..core.ids import iso, now_utc, short_job_name
from ..core.models import (
    JobHandle,
    JobSpec,
    JobStatus,
    NodeSpec,
    PlannedCommand,
    ResourceRequirements,
    RunState,
    SlurmSpec,
    SubmissionReceipt,
)
from ..core.runner import shquote
from ..progress.pilot import pilot_command, pilot_files, pilot_interpreter, pilot_timeout
from ..resources import parsers
from ..storage.output_probe import parse_output_manifest
from .base import BaseExecutor, map_slurm_state
from .scripts import newer_files_command, tail_command
from .ssh import SshTransport

_JOB_ID_RE = re.compile(r"^\s*(\d+)")


def format_time_limit(minutes: int) -> str:
    days, remainder = divmod(int(minutes), 60 * 24)
    hours, mins = divmod(remainder, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:00"
    return f"{hours:02d}:{mins:02d}:00"


def build_sbatch_script(
    *,
    job: JobSpec,
    run_dir: str,
    slurm: SlurmSpec,
    partition: str | None = None,
) -> str:
    resources: ResourceRequirements = job.resources
    directives = [
        "#!/usr/bin/env bash",
        f"#SBATCH --job-name={short_job_name(job.run_id)}",
    ]
    if partition:
        directives.append(f"#SBATCH --partition={partition}")
    if slurm.account:
        directives.append(f"#SBATCH --account={slurm.account}")
    if slurm.qos:
        directives.append(f"#SBATCH --qos={slurm.qos}")
    minutes = job.time_minutes or resources.time_minutes
    if minutes:
        directives.append(f"#SBATCH --time={format_time_limit(minutes)}")
    if resources.tasks > 1:
        per_task = max(1, (resources.cpus or resources.tasks) // resources.tasks)
        directives.append(f"#SBATCH --ntasks={resources.tasks}")
        directives.append(f"#SBATCH --cpus-per-task={per_task}")
    elif resources.cpus:
        directives.append(f"#SBATCH --cpus-per-task={resources.cpus}")
    if resources.memory_gb:
        directives.append(f"#SBATCH --mem={resources.memory_gb}G")
    if resources.gpus > 0:
        directives.append(f"#SBATCH --gres={slurm.gpu_gres_template.format(n=resources.gpus)}")
    directives.extend(
        [
            f"#SBATCH --output={posixpath.join(run_dir, slurm.log_pattern)}",
            "# RCF writes the command output to run.log and the exit status to",
            "# exit_code, next to this script, so state survives the scheduler DB.",
        ]
    )

    body: list[str] = [
        "set -o pipefail",
        f"RUN_DIR={shquote(run_dir)}",
        f"cd {shquote(job.command.workdir)} || {{ "
        "printf '%s\\n' 97 > \"$RUN_DIR/.exit_code.tmp\"; "
        "mv \"$RUN_DIR/.exit_code.tmp\" \"$RUN_DIR/exit_code\"; exit 97; }",
        'printf \'%s\\n\' $$ > "$RUN_DIR/pid"',
        'date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/started_at"',
        "echo \"job_id=${SLURM_JOB_ID:-unknown}\" > \"$RUN_DIR/slurm_job\"",
        "echo \"node_list=${SLURM_JOB_NODELIST:-unknown}\" >> \"$RUN_DIR/slurm_job\"",
    ]
    timeout = pilot_timeout(job.metadata)
    setup = [] if timeout is not None else list(job.command.preamble)
    if setup:
        body.append("{")
        body.extend(f"  {line}" for line in setup)
        body.append('} > "$RUN_DIR/env.log" 2>&1')
    else:
        body.append('printf "%s\\n" "no environment setup" > "$RUN_DIR/env.log"')
    for key, value in sorted(({} if timeout is not None else job.command.env).items()):
        body.append(f"export {key}={shquote(value)}")
    command_text = (
        " ".join(shquote(a) for a in job.command.argv)
        if not job.command.shell
        else " ".join(job.command.argv)
    )
    body.extend(
        [
            'printf "# command\\n%s\\n# workdir\\n%s\\n# slurm job\\n%s\\n" '
            + shquote(command_text)
            + " "
            + shquote(job.command.workdir)
            + ' "${SLURM_JOB_ID:-unknown}" > "$RUN_DIR/run.log"',
            "(\n" + (pilot_command(pilot_interpreter(job.command.argv)) if timeout is not None else command_text)
            + '\n) >> "$RUN_DIR/run.log" 2>&1',
            "rc=$?",
            'printf \'%s\\n\' "$rc" > "$RUN_DIR/.exit_code.tmp"',
            'mv "$RUN_DIR/.exit_code.tmp" "$RUN_DIR/exit_code"',
            'date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/finished_at"',
            "exit $rc",
        ]
    )
    return "\n".join(directives + [""] + body) + "\n"


class SlurmExecutor(BaseExecutor):
    kind = "slurm"

    def __init__(
        self,
        node: NodeSpec,
        transport: SshTransport,
        *,
        submit_timeout: float = 180.0,
        query_timeout: float = 60.0,
    ) -> None:
        super().__init__(node.id)
        self.node = node
        self.transport = transport
        self.slurm = node.slurm or SlurmSpec()
        self.submit_timeout = submit_timeout
        self.query_timeout = query_timeout

    # -- submit ------------------------------------------------------------- #
    def submit(self, spec: JobSpec) -> SubmissionReceipt:
        run_dir = spec.run_dir or self.node.runs_root(spec.run_id)
        partition = spec.resources.partition or self.slurm.default_partition
        script = build_sbatch_script(job=spec, run_dir=run_dir, slurm=self.slurm, partition=partition)
        metadata = {
            "run_id": spec.run_id,
            "project_id": spec.project_id,
            "env_id": spec.env_id,
            "executor": "slurm",
            "partition": partition,
            "account": self.slurm.account,
            "resources": spec.resources.model_dump(mode="json"),
            "command": spec.command.model_dump(mode="json"),
            "workdir": spec.command.workdir,
            "result_dir": spec.result_dir,
            "created_at": iso(now_utc()),
            "rcf_schema": 1,
            **spec.metadata,
        }
        upload = " && ".join(
            [
                self.transport.write_file_command(
                    posixpath.join(run_dir, "run.json"),
                    json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True),
                ),
                self.transport.write_file_command(posixpath.join(run_dir, "sbatch.sh"), script),
                *[
                    self.transport.write_file_command(posixpath.join(run_dir, name), text)
                    for name, text in pilot_files(
                        spec.command, pilot_timeout(spec.metadata),
                        calibration_pilot=spec.metadata.get("pilot_requested", True),
                    ).items()
                ],
                self.transport.write_file_command(
                    posixpath.join(run_dir, "params.json"),
                    json.dumps(spec.metadata.get("params", {}), indent=2, ensure_ascii=False, sort_keys=True),
                ),
            ]
        )
        self.transport.run_checked(upload, what="upload run directory", timeout=self.submit_timeout)

        submit_cmd = self._submit_command(run_dir, parsable=True)
        result = self.transport.run(submit_cmd, timeout=self.slurm.submit_timeout)
        job_id: str | None = None
        if result.returncode == 0:
            match = _JOB_ID_RE.match(result.stdout.strip())
            if match:
                job_id = match.group(1)
            else:
                # rc=0 with unparsable output means a job may well exist. Never
                # retry here: a second sbatch would duplicate the work.
                raise SubmissionUncertain(
                    f"sbatch accepted the job on {self.node.id!r} but returned no job id",
                    hint="check `squeue -u $USER` on the cluster before retrying",
                    detail={
                        "stdout": result.stdout.strip()[:400],
                        "stderr": result.stderr.strip()[:400],
                    },
                )
        else:
            # Retry without --parsable only when the flag itself is unknown on
            # this (older) Slurm build. Every other failure is a real rejection.
            unsupported = re.search(
                r"(unrecognized option|invalid option|unknown option).*--parsable",
                result.stderr + result.stdout,
                re.IGNORECASE,
            )
            if result.timed_out or result.returncode in (124, 255):
                raise SubmissionUncertain(
                    "sbatch reply was interrupted; the job may already exist",
                    hint="recover status from the run directory; do not submit again",
                )
            if not unsupported:
                raise SchedulerError(
                    f"sbatch failed on {self.node.id!r}: {result.first_error_line()}",
                    detail={
                        "node_id": self.node.id,
                        "returncode": result.returncode,
                        "stdout": result.stdout.strip()[:400],
                        "stderr": result.stderr.strip()[:400],
                    },
                )
            fallback = self.transport.run(
                self._submit_command(run_dir, parsable=False),
                timeout=self.slurm.submit_timeout,
            )
            match = re.search(r"Submitted batch job (\d+)", fallback.stdout + fallback.stderr)
            if match and fallback.returncode == 0:
                job_id = match.group(1)
            else:
                error_type = (
                    SubmissionUncertain
                    if fallback.returncode in (0, 124, 255) or fallback.timed_out
                    else SchedulerError
                )
                raise error_type(
                    f"sbatch failed on {self.node.id!r}: {fallback.first_error_line()}",
                    detail={
                        "node_id": self.node.id,
                        "returncode": fallback.returncode,
                        "stdout": fallback.stdout.strip()[:400],
                        "stderr": fallback.stderr.strip()[:400],
                    },
                )
        handle = JobHandle(
            kind=self.kind,
            node_id=self.node_id,
            job_id=job_id,
            workdir=run_dir,
            log_path=posixpath.join(run_dir, "run.log"),
            extra={
                "partition": partition,
                "sbatch": self.slurm.commands.sbatch,
                "ssh_alias": self.transport.alias,
                "project_dir": spec.command.workdir,
            },
        )
        return SubmissionReceipt(handle=handle, state=RunState.PENDING, raw=result.stdout.strip())

    def _submit_command(self, run_dir: str, *, parsable: bool) -> str:
        """Persist the receipt before SSH can lose its reply to the controller."""

        flag = " --parsable" if parsable else ""
        return (
            f"cd {shquote(run_dir)} || exit 96\n"
            f"reply=$({self.slurm.commands.sbatch}{flag} sbatch.sh); rc=$?\n"
            'if [ "$rc" -ne 0 ]; then printf "%s\\n" "$reply"; exit "$rc"; fi\n'
            'job_id=$(printf "%s\\n" "$reply" | sed -n '
            + ("'s/^\\([0-9][0-9]*\\).*$/\\1/p'" if parsable else "'s/^Submitted batch job \\([0-9][0-9]*\\).*$/\\1/p'")
            + ')\n'
            'if [ -n "$job_id" ]; then '
            'printf "%s\\n" "$job_id" > .job_id.tmp && mv .job_id.tmp job_id '
            '|| { echo "RCF: could not persist accepted job id" >&2; exit 124; }; fi\n'
            'printf "%s\\n" "$reply"\n'
        )

    # -- observe ------------------------------------------------------------ #
    def _read_file(self, path: str, timeout: float | None = None) -> str | None:
        result = self.transport.run(f"cat {shquote(path)} 2>/dev/null || true", timeout=timeout or self.query_timeout)
        text = result.stdout.strip()
        return text or None

    def status(self, handle: JobHandle) -> JobStatus:
        run_dir = handle.workdir or ""
        # 1) Our own completion marker: the only evidence that survives a
        #    scheduler database purge, and the fastest path for short jobs.
        exit_raw = self._read_file(posixpath.join(run_dir, "exit_code"))
        if exit_raw is not None:
            try:
                code = int(exit_raw)
            except ValueError:
                code = None
            state = RunState.SUCCEEDED if code == 0 else RunState.FAILED
            return JobStatus(
                state=state,
                exit_code=code,
                detail="exit_code file written by the batch script",
                observed_at=now_utc(),
            )

        job_id = handle.job_id
        if not job_id and run_dir:
            recovered = self._read_file(posixpath.join(run_dir, "job_id"))
            if recovered and recovered.isdigit():
                job_id = handle.job_id = recovered
        if not job_id:
            return JobStatus(
                state=RunState.UNKNOWN, detail="handle has no Slurm job id", observed_at=now_utc()
            )

        # 2) Accounting: authoritative exit status once the job is finished.
        sacct = self.transport.run(
            f"{self.slurm.commands.sacct} -j {shquote(job_id)} -P -n -o {parsers.SACCT_FORMAT} 2>/dev/null || true",
            timeout=self.query_timeout,
        )
        job = parsers.parse_sacct(sacct.stdout, job_id=job_id)
        if job is not None:
            state = map_slurm_state(job.state)
            if state.terminal:
                code = parsers.parse_slurm_exit_code(job.exit_code)
                if state is RunState.SUCCEEDED and code not in (0, None):
                    state = RunState.FAILED
                return JobStatus(
                    state=state,
                    exit_code=code,
                    scheduler_state=job.state,
                    detail=f"sacct state={job.state} exit={job.exit_code}",
                    observed_at=now_utc(),
                )

        # 3) Queue: pending/running, with the reason when it is pending.
        squeue = self.transport.run(
            f"{self.slurm.commands.squeue} -h -j {shquote(job_id)} -o \"%T|%M|%N|%R\" 2>/dev/null || true",
            timeout=self.query_timeout,
        )
        line = next((ln.strip() for ln in squeue.stdout.splitlines() if ln.strip()), "")
        if line:
            parts = [p.strip() for p in line.split("|")]
            raw_state = parts[0] if parts else ""
            state = map_slurm_state(raw_state)
            detail = f"squeue state={raw_state}"
            if len(parts) > 3 and parts[3]:
                detail += f" reason={parts[3]}"
            return JobStatus(
                state=state if state is not RunState.UNKNOWN else RunState.PENDING,
                scheduler_state=raw_state,
                node_list=parts[2] if len(parts) > 2 else None,
                detail=detail,
                observed_at=now_utc(),
            )

        # 4) Accounting said something non-terminal (e.g. PENDING); trust it.
        if job is not None:
            state = map_slurm_state(job.state)
            return JobStatus(
                state=state,
                scheduler_state=job.state,
                detail=f"sacct state={job.state} (not in queue)",
                observed_at=now_utc(),
            )

        # 5) Nothing anywhere: the job exists but the scheduler forgot it.
        return JobStatus(
            state=RunState.UNKNOWN,
            detail=(
                "job id is not in squeue or sacct and no exit_code file exists; "
                "the job may have been purged before it started"
            ),
            observed_at=now_utc(),
        )

    def exists(self, handle: JobHandle) -> bool:
        """True when the run directory is still present on the login node."""

        run_dir = shquote(handle.workdir or "")
        probe = self.transport.run(
            f"[ -d {run_dir} ] && echo yes || echo no", timeout=self.query_timeout
        )
        return "yes" in probe.stdout

    def logs(self, handle: JobHandle, *, tail: int = 200) -> str:
        run_dir = handle.workdir or ""
        result = self.transport.run(tail_command(run_dir, tail), timeout=self.query_timeout)
        text = result.stdout
        if "RCF: run.log not found" in text and not self.exists(handle):
            return (
                f"RCF: the run directory {run_dir} no longer exists on {self.node.id!r}; "
                "its log and artifacts were removed outside RCF"
            )
        if "RCF: run.log not found" in text and handle.job_id:
            fallback = self.transport.run(
                f"ls -1t {shquote(run_dir)}/{self.slurm.log_pattern.replace('%j', '*')} 2>/dev/null | head -n 1",
                timeout=self.query_timeout,
            )
            candidate = fallback.stdout.strip().splitlines()
            if candidate:
                text = self.transport.run(
                    f"tail -n {int(tail)} -- {shquote(candidate[0])}", timeout=self.query_timeout
                ).stdout
                text = f"# from {candidate[0]}\n{text}"
        return text

    def cancel(self, handle: JobHandle) -> JobStatus:
        if not handle.job_id:
            return JobStatus(state=RunState.UNKNOWN, detail="no job id", observed_at=now_utc())
        result = self.transport.run(
            f"{self.slurm.commands.scancel} {shquote(handle.job_id)} 2>&1",
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            raise SchedulerError(
                f"scancel failed on {self.node.id!r}: {result.first_error_line()}"
            )
        observed = self.status(handle)
        return JobStatus(
            state=observed.state,
            exit_code=observed.exit_code,
            scheduler_state=observed.scheduler_state,
            detail=f"scancel requested; scheduler now reports {observed.state.value}",
            observed_at=now_utc(),
        )

    def workdir_changes(
        self, handle: JobHandle, *, limit: int = 50, root: str | None = None
    ) -> list[dict[str, object]]:
        """Same diagnostic as the SSH executor (Slurm nodes have shared FS)."""

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


def describe_command(command: PlannedCommand) -> str:  # pragma: no cover - debug helper
    return " ".join(shquote(a) for a in command.argv)
