"""Windows Task Scheduler executor for long jobs inside WSL.

Windows OpenSSH terminates the process tree when the SSH session closes, so a
detached ``setsid`` job does not survive there. A scheduled task is owned by the
Windows service instead, while the job still runs inside the same WSL userland
and writes the same run-directory files as every other executor.
"""

from __future__ import annotations

import base64
import json
import posixpath

from ..core.errors import ExecutorError, RcfError
from ..core.ids import iso, now_utc
from ..core.models import (
    JobHandle,
    JobSpec,
    JobStatus,
    NodeSpec,
    RunState,
    SubmissionReceipt,
)
from ..core.runner import shquote
from ..progress.pilot import pilot_files, pilot_interpreter, pilot_timeout
from ..resources import parsers
from .scripts import cancel_command, run_script
from .ssh import SshTransport
from .ssh_exec import SshExecutor

#: Get-ScheduledTask State: 0 unknown, 1 disabled, 2 queued, 3 ready, 4 running.
_TASK_RUNNING = {2, 4}
_TASK_READY = {1, 3}
#: 0x41303: the task has never run.
_NEVER_RUN = 0x41303
_TASK_IN_PROGRESS = 0x41301


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class WindowsTaskExecutor(SshExecutor):
    """SSH for files/probes, Task Scheduler for the actual long-running process."""

    kind = "windows_task"

    def __init__(self, node: NodeSpec, transport: SshTransport) -> None:
        if node.windows_task is None:
            raise ExecutorError(f"node {node.id!r} has no windows_task configuration")
        super().__init__(node, transport)
        self.task = node.windows_task

    # -- naming / PowerShell ------------------------------------------------ #
    def _task_name(self, handle_or_run: JobHandle | str) -> str:
        if isinstance(handle_or_run, JobHandle):
            name = handle_or_run.job_id or handle_or_run.extra.get("task_name")
            run_id = posixpath.basename((handle_or_run.workdir or "").rstrip("/"))
        else:
            name, run_id = None, handle_or_run
        name = str(name or (self.task.task_prefix + run_id if run_id else ""))
        # TaskName supports wildcard matching. Reject paths/patterns even on
        # recovered handles, so an operation cannot target multiple tasks.
        if not name or not all(c.isalnum() or c in "._-" for c in name):
            raise ExecutorError(f"invalid scheduled task name: {name!r}")
        return name

    def _windows_path(self, posix_path: str) -> str:
        """Translate the WSL path to the path cmd.exe/Task Scheduler sees."""
        work_root = self.node.work_root.rstrip("/")
        if self.task.windows_work_root and (
            posix_path == work_root or posix_path.startswith(work_root + "/")
        ):
            suffix = posix_path[len(work_root):].replace("/", "\\")
            return self.task.windows_work_root + suffix
        for prefix, replacement in sorted(
            self.node.scp_path_map.items(), key=lambda item: len(item[0]), reverse=True
        ):
            root = prefix.rstrip("/")
            if posix_path == root or posix_path.startswith(root + "/"):
                suffix = posix_path[len(root):].replace("/", "\\")
                return replacement.replace("/", "\\").rstrip("\\") + suffix
        raise ExecutorError(
            f"cannot map {posix_path!r} to a Windows path on {self.node.id!r}",
            hint="set [nodes.<id>].windows_task.windows_work_root or scp_path_map",
        )

    @staticmethod
    def _cmd_quote(path: str) -> str:
        # cmd expands %VAR% even inside quotes; such paths are rejected rather
        # than silently redirected to the wrong file.
        if any(char in path for char in '"%\r\n\x00'):
            raise ExecutorError(
                "Windows task paths may not contain quotes, percent signs or newlines",
                detail={"path": path},
            )
        return f'"{path}"'

    def _powershell(self, script: str, *, timeout: float = 30.0) -> str:
        program = shquote(self.task.powershell)
        command = (
            f"{program} -NoProfile -NonInteractive -EncodedCommand {_encoded(script)} < /dev/null"
        )
        result = self.transport.run(command, timeout=timeout)
        if not result.ok:
            raise ExecutorError(
                f"PowerShell task operation failed on {self.node.id!r}: "
                f"{result.first_error_line()}",
                detail={
                    "returncode": result.returncode,
                    "stdout": result.stdout.strip()[:500],
                    "stderr": result.stderr.strip()[:500],
                },
            )
        return result.stdout.strip()

    def _task_lookup(self, handle: JobHandle) -> str:
        name = _ps_quote(self._task_name(handle))
        # Enumerate successfully, then compare exact root names. A missing
        # name and a denied/broken CIM query must never be the same evidence.
        return f"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$name = {name}
$matches = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {{
  $_.TaskPath -eq '\\' -and $_.TaskName -eq $name
}})
if ($matches.Count -gt 1) {{ throw 'ambiguous scheduled task name' }}
$task = if ($matches.Count -eq 1) {{ $matches[0] }} else {{ $null }}
"""

    def _query_task(self, handle: JobHandle) -> dict | None:
        script = self._task_lookup(handle) + """
if ($null -eq $task) {
  Write-Output '{"exists":false}'
  exit 0
}
$info = $task | Get-ScheduledTaskInfo -ErrorAction Stop
if ($null -eq $info) { throw 'missing scheduled task info' }
[pscustomobject]@{exists=$true; state=[int]$task.State; last_result=[int64]$info.LastTaskResult} |
  ConvertTo-Json -Compress
"""
        text = self._powershell(script, timeout=20)
        try:
            payload = json.loads(text.splitlines()[-1])
            if not isinstance(payload, dict) or type(payload.get("exists")) is not bool:
                raise ValueError("missing explicit existence evidence")
            if payload["exists"] and (
                type(payload.get("state")) is not int
                or payload["state"] not in range(5)
                or type(payload.get("last_result")) is not int
            ):
                raise ValueError("invalid task state/result")
        except (ValueError, IndexError) as exc:
            raise ExecutorError(
                f"cannot parse scheduled-task state on {self.node.id!r}: {text[:300]}"
            ) from exc
        return payload if payload["exists"] else None

    def _register_and_start(self, handle: JobHandle, launcher: str) -> None:
        name = _ps_quote(self._task_name(handle))
        # Task Scheduler must not launch wsl.exe directly: in a non-console
        # task context that exits with -1. cmd.exe gives WSL a console and a
        # place to write its launcher log in Task Scheduler sessions.
        action = _ps_quote('/c ' + self._cmd_quote(launcher))
        script = f"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute 'C:\\Windows\\System32\\cmd.exe' -Argument {action}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName {name} -Action $action -Settings $settings `
  -Description 'RCF long-running research job' -Force | Out-Null
Start-ScheduledTask -TaskName {name}
"""
        # Windows interop programs may drain bash -s's script stdin. Never let
        # the task operation consume the SSH command stream.
        self._powershell(script, timeout=45)

    def _stop_task(self, handle: JobHandle) -> None:
        script = self._task_lookup(handle) + """
if ($null -ne $task) { $task | Stop-ScheduledTask -ErrorAction Stop }
"""
        self._powershell(script, timeout=20)

    def _delete_task(self, handle: JobHandle) -> None:
        # Only terminal callers may request cleanup. Recheck in the same
        # PowerShell operation so queued/running/unknown tasks keep evidence.
        script = self._task_lookup(handle) + """
if ($null -ne $task) {
  if ([int]$task.State -notin @(1, 3)) { throw 'task is not confirmed nonrunning' }
  $task | Unregister-ScheduledTask -Confirm:$false -ErrorAction Stop
}
"""
        try:
            self._powershell(script, timeout=20)
        except RcfError:
            # Cleanup is best effort only after independent terminal evidence;
            # denied/failed cleanup leaves the diagnostic task intact.
            pass

    # -- submit ------------------------------------------------------------- #
    def submit(self, spec: JobSpec) -> SubmissionReceipt:
        self._task_name(spec.run_id)
        run_dir = spec.run_dir or self.node.runs_root(spec.run_id)
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
            "executor": "windows_task",
            "resources": spec.resources.model_dump(mode="json"),
            "command": spec.command.model_dump(mode="json"),
            "workdir": spec.command.workdir,
            "result_dir": spec.result_dir,
            "created_at": iso(now_utc()),
            "rcf_schema": 1,
            **spec.metadata,
        }
        task_script = "\n".join(
            [
                "#!/usr/bin/env bash",
                f"RUN_DIR={shquote(run_dir)}",
                'cd "$RUN_DIR" || exit 97',
                # cmd.exe already owns the rcf-task.log redirect; reopening it
                # from WSL fails on Windows shared-file semantics.
                "bash run.sh",
                "",
            ]
        )
        windows_run_dir = self._windows_path(run_dir)
        launcher = windows_run_dir + "\\rcf-task.cmd"
        wsl_script = posixpath.join(run_dir, "rcf-task.sh")
        # Do not quote the distro: cmd passes the quotes literally in the
        # Task Scheduler context and WSL reports WSL_E_DISTRO_NOT_FOUND.
        distro = f"-d {self.task.distro} " if self.task.distro else ""
        launcher_text = "\r\n".join(
            [
                "@echo off",
                f"cd /d {self._cmd_quote(windows_run_dir)}",
                f"{self._cmd_quote(self.task.wsl_exe)} {distro}-- /bin/bash {self._cmd_quote(wsl_script)} "
                f"> {self._cmd_quote(windows_run_dir + chr(92) + 'rcf-task.log')} 2>&1",
                'set "rc=%ERRORLEVEL%"',
                f"echo rc=%rc% > {self._cmd_quote(windows_run_dir + chr(92) + 'rcf-task.rc')}",
                "exit /b %rc%",
                "",
            ]
        )
        upload = " && ".join(
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
                self.transport.write_file_command(
                    posixpath.join(run_dir, "rcf-task.sh"), task_script,
                ),
                self.transport.write_file_command(
                    posixpath.join(run_dir, "rcf-task.cmd"), launcher_text,
                ),
            ]
        )
        uploaded = self.transport.run(upload, timeout=self.submit_timeout)
        if uploaded.returncode != 0:
            raise ExecutorError(
                f"failed to upload the Windows task run directory on {self.node.id!r}: "
                f"{uploaded.first_error_line()}",
                detail={"stderr": uploaded.stderr.strip()[:600]},
            )
        handle = JobHandle(
            kind=self.kind,
            node_id=self.node_id,
            job_id=self._task_name(spec.run_id),
            workdir=run_dir,
            log_path=posixpath.join(run_dir, "run.log"),
            extra={
                "task_name": self._task_name(spec.run_id),
                "wsl_distro": self.task.distro,
                "project_dir": spec.command.workdir,
            },
        )
        self._register_and_start(handle, launcher)
        return SubmissionReceipt(
            handle=handle,
            state=RunState.PENDING,
            raw=f"windows_task={handle.job_id}",
        )

    # -- observe ------------------------------------------------------------ #
    def status(self, handle: JobHandle) -> JobStatus:
        data, probe = self._probe(handle)
        if not probe.ok:
            return JobStatus(state=RunState.UNKNOWN, detail="WSL status probe failed",
                             observed_at=now_utc())
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
                return JobStatus(state=RunState.UNKNOWN, detail="invalid exit_code file",
                                 observed_at=now_utc())
            state = RunState.SUCCEEDED if code == 0 else RunState.FAILED
            self._delete_task(handle)
            return JobStatus(
                state=state,
                exit_code=code,
                detail="; ".join(detail_bits) or "exit_code file present",
                observed_at=now_utc(),
                finished_at=self._parse_stamp(finished_at),
            )
        if state_raw == "RUNNING":
            if data.get("pid_marker") == "mismatch":
                return JobStatus(
                    state=RunState.UNKNOWN,
                    detail="pid file points at a different process (pid reuse)",
                    observed_at=now_utc(),
                )
            return JobStatus(
                state=RunState.RUNNING,
                detail="; ".join(detail_bits) or f"pid {data.get('pid')} alive",
                observed_at=now_utc(),
            )
        if state_raw in {"LOST", "MISSING"}:
            task = self._query_task(handle)
            nonrunning = task is None or task["state"] in _TASK_READY
            if state_raw == "LOST" and data.get("pid") and nonrunning:
                self._delete_task(handle)
                return JobStatus(
                    state=RunState.LOST,
                    detail="WSL pid is gone and scheduled task is absent/nonrunning",
                    observed_at=now_utc(),
                )
            return JobStatus(
                state=RunState.UNKNOWN,
                detail="WSL process/run directory evidence and scheduler state are inconclusive",
                observed_at=now_utc(),
            )
        if state_raw != "STARTING":
            raise ExecutorError(
                f"status probe on {self.node.id!r} returned no supported state",
                detail={"stdout": str(data)[:300]},
            )

        task = self._query_task(handle)
        # A prior terminal observation may have removed the scheduler entry;
        # the durable launcher receipt must still classify bootstrap failure.
        task_state = task["state"] if task is not None else 3
        last_result = task.get("last_result") if task is not None else None
        if task_state in _TASK_RUNNING:
            return JobStatus(
                state=RunState.PENDING,
                detail="scheduled task queued/running; run.sh has not written a pid yet",
                observed_at=now_utc(),
            )
        if task_state in _TASK_READY:
            launcher = self.transport.run(
                self._file_tail_command(posixpath.join(handle.workdir or "", "rcf-task.rc"), 1),
                timeout=10,
            )
            launcher_code = None
            if launcher.ok and launcher.stdout.strip().startswith("rc="):
                try:
                    launcher_code = int(launcher.stdout.strip()[3:])
                except ValueError:
                    pass
            # A completed launcher with no run.sh pid/exit receipt failed to
            # bootstrap, even if an old cmd wrapper reported Windows result 0.
            if launcher_code is not None or last_result not in (None, 0, _NEVER_RUN, _TASK_IN_PROGRESS):
                if launcher_code is not None:
                    self._delete_task(handle)
                # Without a durable rc, keep the scheduler's diagnostic result.
                return JobStatus(
                    state=RunState.FAILED,
                    exit_code=launcher_code,
                    detail=(f"scheduled task finished with Windows result {last_result} "
                            f"before writing a pid; launcher rc={launcher_code}"),
                    observed_at=now_utc(),
                )
        return JobStatus(
            state=RunState.UNKNOWN,
            detail="scheduled task is no longer running but run.sh left no pid or exit code",
            observed_at=now_utc(),
        )

    # -- logs / cancel ------------------------------------------------------ #
    def logs(self, handle: JobHandle, *, tail: int = 200) -> str:
        text = super().logs(handle, tail=tail)
        if "run.log not found" not in text:
            return text
        run_dir = handle.workdir or ""
        fallback = self.transport.run(
            self._file_tail_command(posixpath.join(run_dir, "rcf-task.log"), tail),
            timeout=self.query_timeout,
        )
        if not fallback.ok:
            raise ExecutorError("cannot read Windows task launcher log",
                                detail={"stderr": fallback.stderr[:300]})
        if "RCF: file not found" in fallback.stdout:
            return "RCF: neither run.log nor rcf-task.log is present"
        return fallback.stdout

    def cancel(self, handle: JobHandle) -> JobStatus:
        def unknown(reason: str) -> JobStatus:
            return JobStatus(state=RunState.UNKNOWN, detail=reason, observed_at=now_utc())

        try:
            data, probe = self._probe(handle)
            state_raw = (data.get("state") or "").upper()
            if not probe.ok:
                return unknown("WSL cancellation probe failed")
            if state_raw == "EXITED":
                return self.status(handle)

            pid = data.get("pid") or ""
            acknowledged = False
            # Never signal a stale/unidentified pid, including when the task
            # query is unavailable. cancel_command itself is not a receipt.
            if pid and pid.isdigit() and state_raw == "RUNNING" and data.get("pid_marker") == "ok":
                result = self.transport.run(
                    cancel_command(handle.workdir or ""), timeout=self.query_timeout + 30,
                )
                outcome = parsers.parse_key_value_block(result.stdout).get("cancelled", "")
                acknowledged = result.ok and outcome in {"term", "force"}
            self._stop_task(handle)
            task = self._query_task(handle)
            if task is not None and task["state"] not in _TASK_READY:
                return unknown("scheduled task is not confirmed absent/nonrunning after stop")
            after, checked = self._probe(handle)
            if not checked.ok:
                return unknown("WSL process disappearance could not be checked")
            if after.get("state", "").upper() == "EXITED":
                return self.status(handle)
            if (acknowledged and pid and after.get("pid") == pid
                    and after.get("state", "").upper() == "LOST"):
                self._delete_task(handle)
                return JobStatus(
                    state=RunState.CANCELLED,
                    detail="scheduled task absent/nonrunning and acknowledged WSL pid is gone",
                    observed_at=now_utc(),
                )
            return unknown("cancellation lacks matching WSL process disappearance evidence")
        except RcfError as exc:
            # A failed stop/query/probe is uncertainty, never a terminal receipt.
            return unknown(f"cancellation not confirmed: {exc}")

    @staticmethod
    def _file_tail_command(path: str, tail: int) -> str:
        """Decode on the host before SSH's UTF-8 decoder sees raw Windows bytes.

        Read at most 1 MiB, including for a single unbounded log line. The
        remote helper deliberately uses only Python 3.6-compatible syntax.
        """
        script = r'''
import os
import sys
path, count = sys.argv[1], int(sys.argv[2])
try:
    stream = open(path, 'rb')
except FileNotFoundError:
    sys.stdout.buffer.write(b'RCF: file not found\n')
else:
    with stream:
        head = stream.read(4096)
        if head.startswith(b'\xff\xfe'):
            encoding = 'utf-16-le'
        elif head.startswith(b'\xfe\xff'):
            encoding = 'utf-16-be'
        elif b'\x00' in head:
            encoding = 'utf-16-le'
        else:
            try:
                # A prefix may end inside a multibyte UTF-8 character.
                import codecs
                codecs.getincrementaldecoder('utf-8-sig')().decode(head, final=False)
                encoding = 'utf-8-sig'
            except UnicodeDecodeError:
                encoding = 'utf-16-le'
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - 1048576)
        if encoding.startswith('utf-16'):
            start += start % 2
        stream.seek(start)
        text = stream.read(1048576).decode(encoding, errors='replace').lstrip('\ufeff')
        lines = text.splitlines(keepends=True)
        if start and lines:
            lines = lines[1:]
        sys.stdout.buffer.write(''.join(lines[-count:] if count else []).encode('utf-8'))
'''
        return f"python3 -c {shquote(script)} {shquote(path)} {min(max(int(tail), 0), 10000)}"

    @staticmethod
    def _parse_stamp(value: str | None):
        from .ssh_exec import _parse_stamp

        return _parse_stamp(value)
