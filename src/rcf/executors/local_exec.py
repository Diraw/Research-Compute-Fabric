"""Local executor: runs a job on the machine hosting RCF.

The job must survive the CLI process, so the command is not a child of the CLI
invocation: a small supervisor script is spawned in its own session and writes
the same on-disk state file set as the remote executor (``pid``, ``started_at``,
``exit_code``, ``run.log``).  That uniformity is what lets the Run Manager
restore state for every backend after a restart.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

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
from ..core.runner import Runner, spawn_detached
from ..progress.pilot import pilot_files, pilot_timeout
from ..storage.output_probe import local_output_manifest
from .base import BaseExecutor

SUPERVISOR = r'''"""RCF job supervisor (generated).

Runs one command, in its own session, with all state written to the run
directory.  Never edited by hand: it is rewritten from src/rcf/executors/local_exec.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback


def _write(run_dir: str, name: str, text: str) -> None:
    tmp = os.path.join(run_dir, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, os.path.join(run_dir, name))


def main() -> int:
    run_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as handle:
        spec = json.load(handle)
    _write(run_dir, "pid", str(os.getpid()))
    _write(run_dir, "started_at", spec["started_at"])
    workdir = spec["workdir"]
    log_path = os.path.join(run_dir, "run.log")
    if not os.path.isdir(workdir):
        _write(run_dir, "run.log", "RCF: workdir does not exist: %s\n" % workdir)
        _write(run_dir, "exit_code", "97")
        _write(run_dir, "finished_at", spec["started_at"])
        return 97
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    argv = list(spec["argv"])
    shell = bool(spec.get("shell"))
    pilot = spec.get("metadata", {}).get("pilot_seconds") is not None
    if pilot:
        argv = [sys.executable, os.path.join(run_dir, "rcf-pilot.py"),
                os.path.join(run_dir, "pilot-args.json")]
        shell = False
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("# command\n%s\n# workdir\n%s\n" % (" ".join(argv), workdir))
        log.flush()
        try:
            # A shell command is already rendered syntax, not one executable
            # argument. Windows list2cmdline would quote the entire expression.
            command = argv[0] if shell and len(argv) == 1 else argv
            completed = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=shell,
                check=False,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            code = completed.returncode
        except FileNotFoundError as exc:
            log.write("RCF: executable not found: %s\n" % (exc,))
            code = 127
        except OSError as exc:
            log.write("RCF: failed to start: %s\n" % (exc,))
            code = 126
        except Exception:
            log.write(traceback.format_exc())
            code = 126
    _write(run_dir, "exit_code", str(code))
    _write(run_dir, "finished_at", __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()))
    return code


if __name__ == "__main__":
    sys.exit(main())
'''


def _stamp(value: str | None):
    """Parse a UTC stamp from the run directory; never raise."""

    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


class LocalExecutor(BaseExecutor):
    kind = "local"

    def __init__(self, node: NodeSpec, runs_root: Path, runner: Runner) -> None:
        super().__init__(node.id)
        self.node = node
        self.runs_root = runs_root
        self.runner = runner

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id

    def submit(self, spec: JobSpec) -> SubmissionReceipt:
        run_dir = Path(spec.run_dir) if spec.run_dir else self.run_dir(spec.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        files = pilot_files(spec.command, pilot_timeout(spec.metadata), posix_shell=False,
                                          calibration_pilot=spec.metadata.get("pilot_requested", True))
        for name, text in files.items():
            (run_dir / name).write_text(text, encoding="utf-8")
        supervisor = run_dir / "supervisor.py"
        supervisor.write_text(SUPERVISOR, encoding="utf-8")
        payload = {
            "run_id": spec.run_id,
            "argv": spec.command.argv,
            "shell": spec.command.shell,
            "workdir": spec.command.workdir,
            "env": spec.command.env,
            "started_at": iso(now_utc()) or "",
            "metadata": spec.metadata,
        }
        (run_dir / "run.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        (run_dir / "params.json").write_text(
            json.dumps(spec.metadata.get("params", {}), indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        try:
            pid = spawn_detached(
                [os.sys.executable, str(supervisor)],
                cwd=str(run_dir),
                stdout_path=str(run_dir / "bootstrap.log"),
                stderr_path=str(run_dir / "bootstrap.log"),
            )
        except OSError as exc:  # pragma: no cover - platform specific
            raise ExecutorError(f"failed to spawn local job: {exc}") from exc
        handle = JobHandle(
            kind=self.kind,
            node_id=self.node_id,
            job_id=str(pid),
            workdir=str(run_dir),
            log_path=str(run_dir / "run.log"),
            extra={
                "supervisor_pid": pid,
                "run_dir": str(run_dir),
                "project_dir": spec.command.workdir,
            },
        )
        return SubmissionReceipt(
            handle=handle,
            state=RunState.PENDING,
            raw=f"spawned supervisor pid={pid}",
        )

    # -- observation -------------------------------------------------------- #
    def _read(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None

    def _pid(self, handle: JobHandle) -> int | None:
        run_dir = Path(handle.workdir or "")
        raw = self._read(run_dir / "pid")
        if raw and raw.isdigit():
            return int(raw)
        return None

    @staticmethod
    def _alive(pid: int) -> bool:
        """True when the process still exists.

        Windows needs ``OpenProcess``: ``os.kill(pid, 0)`` raises ``SystemError``
        for processes owned by another session (and for the detached children we
        start ourselves).
        """

        if pid <= 0:
            return False
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - process owned by another user
            return True
        except OSError:  # pragma: no cover
            return False
        return True

    def status(self, handle: JobHandle) -> JobStatus:
        run_dir = Path(handle.workdir or "")
        exit_raw = self._read(run_dir / "exit_code")
        if exit_raw is not None:
            try:
                code = int(exit_raw)
            except ValueError:
                code = None
            state = RunState.SUCCEEDED if code == 0 else RunState.FAILED
            return JobStatus(
                state=state,
                exit_code=code,
                detail="exit_code file",
                observed_at=now_utc(),
                finished_at=_stamp(self._read(run_dir / "finished_at")),
            )
        pid = self._pid(handle)
        if pid is None:
            return JobStatus(
                state=RunState.PENDING,
                detail="supervisor has not written its pid yet",
                observed_at=now_utc(),
            )
        if self._alive(pid):
            return JobStatus(state=RunState.RUNNING, detail=f"pid {pid} alive", observed_at=now_utc())
        return JobStatus(
            state=RunState.LOST,
            detail="process is gone but no exit code was written (killed or host restarted)",
            observed_at=now_utc(),
        )

    def logs(self, handle: JobHandle, *, tail: int = 200) -> str:
        run_dir = Path(handle.workdir or "")
        log_path = Path(handle.log_path or (run_dir / "run.log"))
        if not log_path.is_file():
            if not run_dir.is_dir():
                return (
                    f"RCF: the run directory {run_dir} no longer exists; "
                    "its log and artifacts were removed outside RCF"
                )
            return "RCF: run.log not found"
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max(1, int(tail)) :])

    def cancel(self, handle: JobHandle) -> JobStatus:
        pid = self._pid(handle)
        if pid is None:
            return JobStatus(state=RunState.UNKNOWN, detail="no pid recorded yet", observed_at=now_utc())
        if not self._alive(pid):
            return self.status(handle)
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError) as exc:
                    raise ExecutorError(f"cannot signal pid {pid}: {exc}") from exc
        else:  # pragma: no cover - Windows
            self.runner.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=30)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self._alive(pid):
            time.sleep(0.1)
        if self._alive(pid) and os.name == "posix":  # pragma: no cover - stubborn process
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self._alive(pid):
            return JobStatus(
                state=RunState.UNKNOWN,
                detail=f"pid {pid} still alive after cancellation request",
                observed_at=now_utc(),
            )
        state = self.status(handle)
        return JobStatus(
            state=RunState.CANCELLED if state.state is not RunState.SUCCEEDED else state.state,
            exit_code=state.exit_code,
            detail=f"cancel requested for pid {pid}",
            observed_at=now_utc(),
        )

    def workdir_changes(
        self, handle: JobHandle, *, limit: int = 50, root: str | None = None
    ) -> list[dict[str, object]]:
        project_root = root or str((handle.extra or {}).get("project_dir") or "")
        if not project_root:
            return []
        return local_output_manifest(
            Path(project_root), limit=limit, reference=Path(handle.workdir or "") / "started_at"
        )

    def result_manifest(
        self, handle: JobHandle, *, limit: int = 200, root: str | None = None,
        hashes: bool = False,
    ) -> list[dict[str, object]]:
        """List complete regular-file outputs, or fail explicitly on overflow."""
        return local_output_manifest(
            Path(root) if root else Path(handle.workdir or "") / "outputs",
            limit=limit, hashes=hashes,
        )
