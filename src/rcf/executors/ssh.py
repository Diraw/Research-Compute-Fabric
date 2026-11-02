"""SSH transport: one place where ``ssh`` is invoked.

Design constraints taken from the project brief:

* Refuse to weaken host key verification.  RCF adds no option that accepts an
  unknown/rotated host key, and it fails closed with an actionable error.
* Batch mode only: an Agent must never block on a password prompt.
* No daemon on the remote side, no admin rights, no tunnel creation.
* The remote side is POSIX sh/bash.  Commands are sent base64-encoded where
  quoting would otherwise be fragile.
"""

from __future__ import annotations

import base64
import posixpath
from pathlib import Path

from ..core.errors import ExecutorError
from ..core.models import NodeSpec
from ..core.runner import CommandResult, Runner, classify_ssh_failure, shquote
from ..security.policy import normalize_ssh_options
from ..security.redaction import redact


class SshTransport:
    """Runs a command string on a host described by an ``~/.ssh/config`` alias."""

    def __init__(
        self,
        alias: str,
        runner: Runner,
        *,
        program: tuple[str, ...] = ("ssh",),
        connect_timeout: int = 10,
        server_alive_interval: int = 30,
        server_alive_count_max: int = 3,
        batch_mode: bool = True,
        extra_options: tuple[str, ...] = (),
        scp_program: tuple[str, ...] = (),
        scp_path_map: dict[str, str] | None = None,
    ) -> None:
        if not alias or alias.startswith("-"):
            raise ExecutorError(f"invalid ssh alias {alias!r}")
        self.alias = alias
        self.runner = runner
        self.program = program or ("ssh",)
        self.connect_timeout = connect_timeout
        self.server_alive_interval = server_alive_interval
        self.server_alive_count_max = server_alive_count_max
        self.batch_mode = batch_mode
        self.extra_options = normalize_ssh_options(list(extra_options))
        self.scp_program = tuple(scp_program) or self._derive_scp_program()
        # Path space used by scp, which on a Windows host is not the same as the
        # shell's (WSL sees /mnt/e, Windows scp sees E:/).
        self.scp_path_map = dict(scp_path_map or {})

    @classmethod
    def from_node(cls, node: NodeSpec, runner: Runner) -> SshTransport:
        if not node.ssh_alias:
            raise ExecutorError(f"node {node.id!r} has no ssh_alias")
        options = node.ssh
        return cls(
            alias=node.ssh_alias,
            runner=runner,
            program=tuple(options.program),
            connect_timeout=options.connect_timeout,
            server_alive_interval=options.server_alive_interval,
            server_alive_count_max=options.server_alive_count_max,
            batch_mode=options.batch_mode,
            extra_options=tuple(options.extra_options),
            scp_program=tuple(options.scp_program),
            scp_path_map=node.scp_path_map,
        )

    # -- argv construction -------------------------------------------------- #
    #: The remote side is always entered through bash, with the script on
    #: stdin.  ``ssh host "<script>"`` would hand the script to the user's
    #: *login* shell — fish, csh, zsh — whose syntax is not POSIX.  A real
    #: Ubuntu workstation with fish as login shell failed every `if ...; then`
    #: before this was fixed.
    SHELL_ENTRY = "bash -s"

    def argv(self) -> list[str]:
        argv = list(self.program)
        argv.append("-T")
        if self.batch_mode:
            argv.extend(["-o", "BatchMode=yes"])
        argv.extend(
            [
                "-o",
                f"ConnectTimeout={self.connect_timeout}",
                "-o",
                f"ServerAliveInterval={self.server_alive_interval}",
                "-o",
                f"ServerAliveCountMax={self.server_alive_count_max}",
            ]
        )
        argv.extend(self.extra_options)
        argv.extend(["--", self.alias, self.SHELL_ENTRY])
        return argv

    def describe(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "program": list(self.program),
            "connect_timeout": self.connect_timeout,
            "batch_mode": self.batch_mode,
            "host_key_checking": "openssh-default (never disabled)",
        }

    # -- execution ---------------------------------------------------------- #
    def run(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Run ``command`` remotely.

        A transport-level failure (255, auth, host key, network) is raised;
        a non-zero *remote* exit status is returned to the caller, because an
        inspected node legitimately reports "command not found" for tools it
        does not have.
        """

        # Defence in depth: a script must never carry CRLF to a POSIX shell.
        script = command.replace(chr(13) + chr(10), chr(10))
        result = self.runner.run(self.argv(), stdin=script, timeout=timeout)
        if result.returncode == 255 and not result.stdout.strip():
            raise classify_ssh_failure(result, alias=self.alias)
        return result

    def run_checked(self, command: str, *, what: str, timeout: float | None = None) -> CommandResult:
        result = self.run(command, timeout=timeout)
        if result.returncode == 127 and "command not found" in f"{result.stderr}{result.stdout}".lower():
            raise ExecutorError(
                f"{self.alias!r} has no bash: {redact(result.first_error_line())}",
                hint="RCF runs remote scripts with `bash -s`; install bash on the node",
                detail={"alias": self.alias},
            )
        if result.returncode != 0:
            raise ExecutorError(
                f"{what} failed on {self.alias!r}: {redact(result.first_error_line())}",
                detail={
                    "alias": self.alias,
                    "returncode": result.returncode,
                    "stderr": redact(result.stderr)[:600],
                },
            )
        return result

    def _derive_scp_program(self) -> tuple[str, ...]:
        """``/usr/bin/ssh`` → ``/usr/bin/scp``; anything else → plain ``scp``."""

        program = list(self.program)
        last = program[-1]
        if last.endswith("ssh"):
            program[-1] = last[: -len("ssh")] + "scp"
            return tuple(program)
        return ("scp",)

    def scp_argv(self, remote_spec: str, local_path: str, *, legacy: bool = False) -> list[str]:
        """argv for copying a remote path down (same hardening as ssh)."""

        argv = list(self.scp_program)
        if self.batch_mode:
            argv.extend(["-o", "BatchMode=yes"])
        argv.extend(
            [
                "-o",
                f"ConnectTimeout={self.connect_timeout}",
                "-o",
                f"ServerAliveInterval={self.server_alive_interval}",
                "-o",
                f"ServerAliveCountMax={self.server_alive_count_max}",
            ]
        )
        argv.extend(self.extra_options)
        if legacy:
            # Old scp (< 9.0) speaks the SCP protocol and lets the remote shell
            # expand the path, so it must be quoted there.
            argv.append("-O")
            argv.extend([remote_spec, local_path])
        else:
            # Modern scp uses SFTP, where the path is taken literally.
            argv.extend([remote_spec, local_path])
        return argv

    def _scp_remote(self, remote_path: str) -> str:
        """Translate a POSIX path into the path space scp speaks on this host."""

        for prefix, replacement in self.scp_path_map.items():
            if remote_path == prefix:
                return replacement if replacement.endswith("/") else replacement + "/"
            if remote_path.startswith(prefix + "/"):
                return replacement.rstrip("/") + remote_path[len(prefix) :]
        return remote_path

    def fetch_file(self, remote_path: str, local_path: Path, *, timeout: float = 1800) -> None:
        """Copy one remote file to the control machine."""

        spec = f"{self.alias}:{self._scp_remote(remote_path)}"
        result = self.runner.run(self.scp_argv(spec, str(local_path)), timeout=timeout)
        if result.returncode != 0:
            quoted = f"{self.alias}:{shquote(self._scp_remote(remote_path))}"
            result = self.runner.run(
                self.scp_argv(quoted, str(local_path), legacy=True), timeout=timeout
            )
        if result.returncode != 0:
            raise ExecutorError(
                f"scp failed for {self.alias!r}: {redact(result.first_error_line())}",
                hint="check that scp exists locally and that the remote path is readable",
                detail={"stderr": redact(result.stderr)[:400]},
            )
        if not local_path.exists():
            raise ExecutorError("scp reported success but no file arrived")

    def push_file(self, local_path: Path, remote_path: str, *, timeout: float = 1800) -> None:
        """Copy one local file to the node (the mirror of :meth:`fetch_file`)."""

        # scp decides "remote or local" by looking for a colon before the first
        # slash, and a Windows path such as C:/Users/... matches that pattern: it
        # would be treated as a *remote* filename.  Running from the file's own
        # directory and passing the bare name sidesteps the heuristic entirely.
        argv = list(self.scp_argv(f"{self.alias}:{self._scp_remote(remote_path)}", local_path.name))
        argv[-2], argv[-1] = argv[-1], argv[-2]
        result = self.runner.run(argv, cwd=str(local_path.parent), timeout=timeout)
        if result.returncode != 0:
            quoted = f"{self.alias}:{shquote(self._scp_remote(remote_path))}"
            argv = list(self.scp_argv(quoted, local_path.name, legacy=True))
            argv[-2], argv[-1] = argv[-1], argv[-2]
            result = self.runner.run(argv, cwd=str(local_path.parent), timeout=timeout)
        if result.returncode != 0:
            raise ExecutorError(
                f"scp upload failed for {self.alias!r}: {redact(result.first_error_line())}",
                hint="check that the target directory exists and is writable",
                detail={"stderr": redact(result.stderr)[:400]},
            )

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def b64(script: str) -> str:
        return base64.b64encode(script.encode("utf-8")).decode("ascii")

    @staticmethod
    def write_file_command(path: str, content: str) -> str:
        """Build a command that atomically writes ``content`` to ``path``.

        base64 avoids every quoting problem (quotes, ``$``, backslashes) and
        works on CentOS 7 with the stock ``base64`` binary.
        """

        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        directory = posixpath.dirname(path)
        return (
            f"mkdir -p {path_quote(directory)} && "
            f"printf %s {path_quote(payload)} | base64 -d > {path_quote(path)}.tmp && "
            f"mv {path_quote(path)}.tmp {path_quote(path)}"
        )


def path_quote(value: str) -> str:
    from ..core.runner import shquote

    return shquote(value)
