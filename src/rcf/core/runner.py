"""The single seam through which every external process is launched.

Business modules never call :mod:`subprocess` directly.  They receive a
:class:`Runner` and hand it an argv list.  That keeps remote command execution
in exactly one place and makes the whole platform testable with a fake runner.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import (
    AuthFailed,
    CommandTimedOut,
    ExecutableMissing,
    ExecutorError,
    HostKeyRejected,
    HostUnreachable,
)

#: Markers OpenSSH prints on stderr, used to classify failures precisely.
HOST_KEY_MARKERS = (
    "host key verification failed",
    "remote host identification has changed",
    "no matching host key type found",
)
AUTH_MARKERS = (
    "permission denied (publickey",
    "permission denied (password",
    "too many authentication failures",
    "no supported authentication methods",
)
UNREACHABLE_MARKERS = (
    "could not resolve hostname",
    "connection refused",
    "connection timed out",
    "timed out",
    "no route to host",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "connection closed by",
    "broken pipe",
    "kex_exchange_identification",
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def combined(self) -> str:
        parts = [p for p in (self.stdout.strip(), self.stderr.strip()) if p]
        return "\n".join(parts)

    def first_error_line(self) -> str:
        for line in (self.stderr or self.stdout or "").splitlines():
            if line.strip():
                return line.strip()
        return self.error or f"exit code {self.returncode}"

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_s": round(self.duration_s, 3),
            "timed_out": self.timed_out,
        }


@runtime_checkable
class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """Real runner.  Never uses a shell; argv is always explicit."""

    def __init__(self, *, default_timeout: float = 60.0) -> None:
        self.default_timeout = default_timeout

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = [str(a) for a in argv]
        if not args:
            raise ExecutorError("empty argv")
        program = args[0]
        resolved = shutil.which(program) if not os.path.isabs(program) else program
        if not resolved or (os.path.isabs(program) and not os.path.exists(program)):
            raise ExecutableMissing(
                f"executable not found: {program!r}",
                hint="install it or point RCF at the right binary in config",
                detail={"program": program},
            )
        merged_env = None
        if env:
            merged_env = {**os.environ, **{k: str(v) for k, v in env.items()}}
        started = time.monotonic()
        # Pipes stay binary on purpose: text mode rewrites LF as CRLF on
        # Windows, which turns a shell script sent to a remote bash into a
        # syntax error. Scripts must arrive byte-for-byte.
        payload = stdin.encode("utf-8") if stdin is not None else None
        try:
            limit = self.default_timeout if timeout is None else timeout
            if os.name == "nt":
                from ._windows_process import run_managed

                completed = run_managed(
                    args, payload=payload, timeout=limit, cwd=cwd, env=merged_env,
                )
            else:
                completed = subprocess.run(
                    args, input=payload, capture_output=True, timeout=limit,
                    cwd=cwd, env=merged_env, check=False,
                )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            return CommandResult(
                argv=tuple(args),
                returncode=124,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                duration_s=elapsed,
                timed_out=True,
                error=f"timed out after {limit:.0f}s",
            )
        except OSError as exc:  # pragma: no cover - platform specific
            raise ExecutorError(f"failed to launch {program!r}: {exc}") from exc
        return CommandResult(
            argv=tuple(args),
            returncode=completed.returncode,
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
            duration_s=time.monotonic() - started,
        )


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> int:
    """Start a process that must outlive the caller; return its pid.

    Used only by the local executor: a compute job cannot be a child of a CLI
    invocation that exits immediately.  Output always goes to files so the
    detached process holds no pipe that could block.
    """

    args = [str(a) for a in argv]
    stdout = open(stdout_path, "ab") if stdout_path else subprocess.DEVNULL  # noqa: SIM115
    stderr = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL  # noqa: SIM115
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows only
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        # Do NOT use DETACHED_PROCESS here. Measured on Windows 11 with a
        # ConPTY-hosted parent (git-bash / Electron): a child created with
        # DETACHED_PROCESS gets a *visible* console window that steals focus,
        # while CREATE_NO_WINDOW gives it an invisible console which its own
        # children inherit (so a job never pops a window either). The child
        # still survives the parent exiting, which is all we need.
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(args, **kwargs)  # type: ignore[arg-type]
    finally:
        if stdout_path:
            stdout.close()  # type: ignore[union-attr]
        if stderr_path:
            stderr.close()  # type: ignore[union-attr]
    return process.pid


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def shquote(value: str) -> str:
    """POSIX-quote a string for safe interpolation into a remote shell command.

    A leading ``~/`` is deliberately left outside the quotes so the remote shell
    still expands it (``~/'my dir'``), while everything after it stays quoted.
    """

    text = str(value)
    if text.startswith("~/"):
        return "~/" + shlex.quote(text[2:])
    return shlex.quote(text)


def join_command(argv: Sequence[str]) -> str:
    return " ".join(shquote(a) for a in argv)


def classify_ssh_failure(result: CommandResult, *, alias: str) -> Exception:
    """Turn a failed ssh invocation into a precise, actionable error."""

    blob = f"{result.stderr}\n{result.stdout}".lower()
    if any(marker in blob for marker in HOST_KEY_MARKERS):
        return HostKeyRejected(
            f"SSH host key for {alias!r} was rejected",
            hint=(
                "RCF never disables host key verification. Verify the fingerprint "
                f"out of band, then run `ssh {alias}` once manually and compare the key."
            ),
            detail={"alias": alias, "stderr": result.stderr.strip()[:400]},
        )
    if any(marker in blob for marker in AUTH_MARKERS):
        return AuthFailed(
            f"SSH authentication to {alias!r} failed",
            hint="check your key/agent and that BatchMode is allowed for this host",
            detail={"alias": alias, "stderr": result.stderr.strip()[:400]},
        )
    if result.timed_out or any(marker in blob for marker in UNREACHABLE_MARKERS):
        return HostUnreachable(
            f"cannot reach {alias!r}",
            hint="check network route / VPN / ssh config, then retry",
            detail={"alias": alias, "stderr": result.stderr.strip()[:400]},
        )
    return ExecutorError(
        f"ssh command failed on {alias!r}: {result.first_error_line()}",
        detail={
            "alias": alias,
            "returncode": result.returncode,
            # ssh can fail before the remote command runs (missing environment,
            # agent pipe, broken config) and then writes only to stderr.  An
            # exit code alone is not diagnosable, so keep ssh's own words.
            "stderr": result.stderr.strip()[:400],
            "stdout": result.stdout.strip()[:200],
        },
    )


def check_timeout(result: CommandResult, *, what: str) -> None:
    if result.timed_out:
        raise CommandTimedOut(
            f"{what} timed out after {result.duration_s:.0f}s",
            hint="increase the timeout in the node probe/transport config if the link is slow",
        )


@dataclass
class ScriptedRunner:
    """Deterministic fake runner used by unit tests and offline demos."""

    responses: list[CommandResult | Exception] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    inputs: list[str | None] = field(default_factory=list)
    default: CommandResult | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        self.inputs.append(stdin)
        if self.responses:
            response = self.responses.pop(0)
        elif self.default is not None:
            response = self.default
        else:
            return CommandResult(argv=args, returncode=0)
        if isinstance(response, Exception):
            raise response
        return CommandResult(
            argv=args,
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
            duration_s=response.duration_s,
            timed_out=response.timed_out,
            error=response.error,
        )

    def expect(self, *responses: CommandResult | Exception) -> ScriptedRunner:
        self.responses.extend(responses)
        return self


def ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(argv=(), returncode=returncode, stdout=stdout, stderr=stderr)


def fail(stderr: str = "boom", returncode: int = 1) -> CommandResult:
    return CommandResult(argv=(), returncode=returncode, stdout="", stderr=stderr)
