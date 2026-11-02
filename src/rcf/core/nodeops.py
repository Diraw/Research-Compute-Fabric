"""Per-node operations that are *not* command execution.

The platform needs to ask small questions about a node ("does this directory
exist?", "what commit is checked out?", "is this path writable?").  Remote nodes
answer through one SSH call; the local node answers with the Python standard
library so Windows works without a POSIX shell.
"""

from __future__ import annotations

from pathlib import Path

from ..executors.scripts import git_state_command
from ..executors.ssh import SshTransport
from ..resources import parsers
from .models import NodeKind, NodeSpec, ProbeStatus
from .runner import CommandResult, Runner


class NodeOps:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def is_remote(node: NodeSpec) -> bool:
        return node.kind is not NodeKind.LOCAL

    def _shell(self, node: NodeSpec, script: str, *, timeout: float = 30.0) -> CommandResult:
        return SshTransport.from_node(node, self.runner).run(script, timeout=timeout)

    # -- queries ------------------------------------------------------------ #
    def dir_exists(self, node: NodeSpec, path: str) -> ProbeStatus:
        if not path:
            return ProbeStatus.UNKNOWN
        if not self.is_remote(node):
            return ProbeStatus.OK if Path(path).is_dir() else ProbeStatus.FAIL
        result = self._shell(
            node,
            f"if [ -d {_q(path)} ]; then echo rcf=dir; "
            f"elif [ -e {_q(path)} ]; then echo rcf=file; else echo rcf=missing; fi",
        )
        data = parsers.parse_key_value_block(result.stdout)
        marker = data.get("rcf")
        return {
            "dir": ProbeStatus.OK,
            "file": ProbeStatus.WARN,
            "missing": ProbeStatus.FAIL,
        }.get(marker or "", ProbeStatus.UNKNOWN)

    def path_writable(self, node: NodeSpec, path: str) -> ProbeStatus:
        if not self.is_remote(node):
            probe = Path(path)
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            return ProbeStatus.OK if probe.exists() and (probe.stat().st_mode & 0o200) else ProbeStatus.WARN
        result = self._shell(
            node,
            f"d={_q(path)}; while [ ! -e \"$d\" ] && [ \"$d\" != / ] && [ \"$d\" != . ]; do "
            "d=$(dirname \"$d\"); done; if [ -w \"$d\" ]; then echo rcf=yes; else echo rcf=no; fi",
        )
        data = parsers.parse_key_value_block(result.stdout)
        return ProbeStatus.OK if data.get("rcf") == "yes" else ProbeStatus.WARN

    def git_state(self, node: NodeSpec, path: str) -> dict[str, str]:
        if not path:
            return {"rcf_repo": "missing"}
        if not self.is_remote(node):
            root = Path(path)
            if not root.is_dir():
                return {"rcf_repo": "missing"}
            if not (root / ".git").exists():
                return {"rcf_repo": "no_git"}
            state = {"rcf_repo": "git"}
            head = self.runner.run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=20)
            if head.ok:
                state["rcf_commit"] = head.stdout.strip()
            branch = self.runner.run(
                ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"], timeout=20
            )
            if branch.ok:
                state["rcf_branch"] = branch.stdout.strip()
            status = self.runner.run(["git", "-C", str(root), "status", "--porcelain"], timeout=30)
            if status.ok:
                state["rcf_dirty"] = "yes" if status.stdout.strip() else "no"
            return state
        result = self._shell(node, git_state_command(path), timeout=45)
        return parsers.parse_key_value_block(result.stdout)

    def disk_avail_kb(self, node: NodeSpec, path: str) -> int | None:
        if not self.is_remote(node):
            import shutil

            probe = Path(path)
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            try:
                return shutil.disk_usage(probe).free // 1024
            except OSError:  # pragma: no cover
                return None
        result = self._shell(node, f"df -Pk {_q(path)} 2>/dev/null | tail -n 1", timeout=30)
        disks = parsers.parse_df(result.stdout)
        return disks[0].avail_kb if disks else None

    def home_dir(self, node: NodeSpec) -> str | None:
        """``$HOME`` on the node, used to resolve ``~/`` in configured paths."""

        if not self.is_remote(node):
            return str(Path.home()).replace("\\", "/")
        result = self._shell(node, 'printf %s "$HOME"')
        found = result.stdout.strip()
        return found or None

    def resolve_inputs(
        self,
        node: NodeSpec,
        cwd: str,
        patterns: list[str],
    ) -> dict[str, list[tuple[str, int]]]:
        """Expand input globs relative to ``cwd`` into ``{pattern: [(path, bytes)]}``.

        Sizes come from the node, not from the control machine: the point is to
        prove the data a run will read is actually there, with the right shape,
        before anything is submitted.  A pattern that matches nothing maps to an
        empty list; the caller decides whether that is fatal.
        """

        out: dict[str, list[tuple[str, int]]] = {pattern: [] for pattern in patterns}
        if not patterns:
            return out
        root = cwd.rstrip("/")
        if not self.is_remote(node):
            base = Path(cwd)
            for pattern in patterns:
                out[pattern] = sorted(
                    (m.relative_to(base).as_posix(), m.stat().st_size)
                    for m in base.glob(pattern)
                    if m.is_file()
                )
            return out
        for pattern in patterns:
            result = self._shell(node, _inputs_list_command(cwd, pattern), timeout=60)
            entries: list[tuple[str, int]] = []
            for line in result.stdout.splitlines():
                size, sep, path = line.partition("	")
                if not sep or not size.strip().isdigit():
                    continue
                relative = path[len(root) + 1 :] if path.startswith(root + "/") else path
                entries.append((relative, int(size)))
            out[pattern] = sorted(entries)
        return out

    def which(self, node: NodeSpec, command: str) -> str | None:
        if not self.is_remote(node):
            import shutil

            return shutil.which(command)
        result = self._shell(node, f"command -v {_q(command)} 2>/dev/null || true")
        found = result.stdout.strip().splitlines()
        return found[0] if found and found[0] else None


def _inputs_list_command(cwd: str, pattern: str) -> str:
    """Stat every file matched by one glob, from inside the checkout.

    The remote shell expands the pattern, so a glob never has to be interpreted
    by the control machine (which may be Windows).
    """

    return (
        f"cd {_q(cwd)} 2>/dev/null || exit 0; "
        f"for p in {pattern}; do "
        '[ -f "$p" ] && printf "%s\t%s\n" "$(wc -c <"$p")" "$p"; '
        "done"
    )


def _q(value: str) -> str:
    from .runner import shquote

    return shquote(value)
