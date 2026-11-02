"""Executor interface shared by local, SSH and Slurm backends.

An executor owns *how* a job is started and observed; it never decides *what*
to run.  All three backends must be able to answer the same four questions
after the controlling process has exited: state, exit code, logs, cancel.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..core.models import (
    JobHandle,
    JobSpec,
    JobStatus,
    PlannedCommand,
    RunState,
    SubmissionReceipt,
)


@runtime_checkable
class Executor(Protocol):
    kind: str
    node_id: str

    def submit(self, spec: JobSpec) -> SubmissionReceipt: ...

    def status(self, handle: JobHandle) -> JobStatus: ...

    def logs(self, handle: JobHandle, *, tail: int = 200) -> str: ...

    def cancel(self, handle: JobHandle) -> JobStatus: ...

    def result_manifest(
        self, handle: JobHandle, *, limit: int = 200, root: str | None = None,
        hashes: bool = False,
    ) -> list[dict[str, Any]]: ...


class BaseExecutor:
    kind = "base"

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id

    # -- helpers shared by subclasses -------------------------------------- #
    @staticmethod
    def render_posix(command: PlannedCommand) -> str:
        """Render a :class:`PlannedCommand` as executable POSIX shell body.

        ``argv`` is quoted element by element: nothing is interpolated as shell
        syntax, so parameters coming from an Agent cannot break out.
        """

        from ..core.runner import join_command

        body = join_command(command.argv) if not command.shell else " ".join(command.argv)
        if command.env:
            exports = "\n".join(
                f"export {key}={_quote_env(value)}" for key, value in sorted(command.env.items())
            )
            body = f"{exports}\n{body}"
        return body

    @staticmethod
    def render_preview(command: PlannedCommand, preamble: list[str] | None = None) -> str:
        lines = ["# environment", *(preamble if preamble is not None else command.preamble)]
        if command.env:
            lines.extend(f"export {key}={_quote_env(value)}" for key, value in sorted(command.env.items()))
        lines.append("# command")
        lines.append(
            " ".join(_shell_word(a) for a in command.argv) if not command.shell else " ".join(command.argv)
        )
        return "\n".join(lines)

    def _status_from_exit(self, exit_code: int | None, *, detail: str = "") -> JobStatus:
        from ..core.ids import now_utc

        if exit_code is None:
            return JobStatus(state=RunState.UNKNOWN, detail=detail, observed_at=now_utc())
        state = RunState.SUCCEEDED if exit_code == 0 else RunState.FAILED
        return JobStatus(state=state, exit_code=exit_code, detail=detail, observed_at=now_utc())


def _quote_env(value: str) -> str:
    from ..core.runner import shquote

    return shquote(str(value))


def _shell_word(value: str) -> str:
    from ..core.runner import shquote

    return shquote(str(value))


SLURM_STATE_TO_RUN: dict[str, RunState] = {
    "PENDING": RunState.PENDING,
    "CONFIGURING": RunState.PENDING,
    "RUNNING": RunState.RUNNING,
    "COMPLETING": RunState.RUNNING,
    "COMPLETED": RunState.SUCCEEDED,
    "FAILED": RunState.FAILED,
    "CANCELLED": RunState.CANCELLED,
    "TIMEOUT": RunState.FAILED,
    "NODE_FAIL": RunState.FAILED,
    "OUT_OF_MEMORY": RunState.FAILED,
    "BOOT_FAIL": RunState.FAILED,
    "DEADLINE": RunState.FAILED,
    "PREEMPTED": RunState.FAILED,
    "REQUEUED": RunState.PENDING,
    "SUSPENDED": RunState.PENDING,
    "UNKNOWN": RunState.UNKNOWN,
}


def parse_find_manifest(text: str) -> list[dict[str, Any]]:
    """Parse ``size<TAB>mtime<TAB>path`` lines produced by GNU find."""

    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split(chr(9))
        if len(parts) != 3 or not parts[0].strip().isdigit():
            continue
        path = parts[2][2:] if parts[2].startswith("./") else parts[2]
        modified = None
        try:
            modified = float(parts[1])
        except ValueError:
            pass
        entries.append({"path": path, "size": int(parts[0]), "modified_epoch": modified})
    return entries


def map_slurm_state(state: str | None) -> RunState:
    if not state:
        return RunState.UNKNOWN
    return SLURM_STATE_TO_RUN.get(state.strip().upper(), RunState.UNKNOWN)
