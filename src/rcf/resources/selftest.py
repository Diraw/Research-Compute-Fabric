"""Node self-test: prove the whole control path works, in one call.

Onboarding a node used to mean "submit a real job and see". Every mismatch found
so far (fish login shell, CRLF in the pipe, a bare ``python`` that does not
exist, an unwritable work root) would have been caught here in ten seconds.

The test is read-only apart from one temporary directory under ``work_root``
which it creates and removes itself. It never submits a job.
"""

from __future__ import annotations

import posixpath
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import Config
from ..core.errors import RcfError
from ..core.ids import now_utc
from ..core.models import Check, NodeKind, NodeSpec, ProbeStatus
from ..core.runner import Runner, shquote
from ..executors.ssh import SshTransport

MARKER = "@@"


def _shell_path(path: str) -> str:
    """Quote a path while leaving a leading ``~/`` expandable by the shell."""

    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shquote(path[2:])
    return shquote(path)


def build_script(*, work_root: str, token: str, has_scheduler: bool) -> str:
    probe_dir = posixpath.join(work_root, f".rcf-selftest-{token}")
    lines = [
        'echo "@@reach"',
        "echo rcf-ok",
        'echo "@@shell"',
        # Multi-line POSIX syntax + a pipeline: catches a non-bash login shell
        # and any CRLF corruption in one go.
        "if [ 1 -eq 1 ]; then",
        "  echo shell-if-ok",
        "else",
        "  echo shell-if-bad",
        "fi",
        "printf '%s' shell-pipe-ok | tr 'a-z' 'a-z'",
        "echo",
        'echo "@@work"',
        f'dir={_shell_path(probe_dir)}',
        'if mkdir -p "$dir" 2>/dev/null; then',
        "  echo work=mkdir-ok",
        '  if printf %s probe > "$dir/probe" 2>/dev/null; '
        'then echo work=write-ok; else echo work=write-fail; fi',
        '  if [ "$(cat "$dir/probe" 2>/dev/null)" = probe ]; then echo work=read-ok; '
        'else echo work=read-fail; fi',
        "  echo work_avail_kb=$(df -Pk \"$dir\" 2>/dev/null | tail -n 1 | awk '{print $4}')",
        '  rm -rf "$dir" && echo cleanup=ok || echo cleanup=fail',
        "else",
        "  echo work=mkdir-fail",
        "fi",
        'echo "@@python"',
        "if command -v python3 >/dev/null 2>&1; then "
        "echo python3=$(python3 -V 2>&1 | tr ' ' '-'); else echo python3=missing; fi",
        'echo "@@gpu"',
        "if command -v nvidia-smi >/dev/null 2>&1; then "
        "echo gpus=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l); "
        "else echo gpus=absent; fi",
        'echo "@@end"',
    ]
    if has_scheduler:
        lines[lines.index('echo "@@gpu"') : lines.index('echo "@@gpu"')] = [
            'echo "@@slurm"',
            "if command -v sbatch >/dev/null 2>&1; then "
            "echo slurm=$(sbatch --version 2>/dev/null | head -n 1); "
            "else echo slurm=missing; fi",
        ]
    return "\n".join(lines)


@dataclass
class SelfTestOutcome:
    node_id: str
    ok: bool
    checks: list[Check] = field(default_factory=list)
    duration_s: float = 0.0
    transcript: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "ok": self.ok,
            "checks": [c.model_dump(mode="json") for c in self.checks],
            "duration_s": round(self.duration_s, 2),
            "transcript": self.transcript[-2000:],
        }


class NodeSelfTest:
    def __init__(self, config: Config, runner: Runner) -> None:
        self.config = config
        self.runner = runner

    def run(self, node: NodeSpec, *, timeout: float = 45.0) -> SelfTestOutcome:
        started = now_utc()
        if node.kind is NodeKind.LOCAL:
            checks = self._local(node)
            duration = (now_utc() - started).total_seconds()
            return SelfTestOutcome(
                node_id=node.id,
                ok=not [c for c in checks if c.status is ProbeStatus.FAIL],
                checks=checks,
                duration_s=duration,
                transcript="local",
            )
        return self._remote(node, timeout=timeout)

    # -- remote ------------------------------------------------------------- #
    def _remote(self, node: NodeSpec, *, timeout: float) -> SelfTestOutcome:
        from ..resources import parsers

        started = now_utc()
        token = secrets.token_hex(4)
        script = build_script(
            work_root=node.work_root, token=token, has_scheduler=True
        )
        transport = SshTransport.from_node(node, self.runner)
        try:
            result = transport.run(script, timeout=timeout)
        except RcfError as exc:
            return SelfTestOutcome(
                node_id=node.id,
                ok=False,
                checks=[Check(name="connect", status=ProbeStatus.FAIL, detail=str(exc)[:300])],
                duration_s=(now_utc() - started).total_seconds(),
            )
        sections = parsers.parse_probe_sections(result.stdout, marker=MARKER)
        checks = _evaluate(
            sections,
            stderr=result.stderr,
            returncode=result.returncode,
            expects_scheduler=node.kind is NodeKind.SLURM,
        )
        return SelfTestOutcome(
            node_id=node.id,
            ok=not [c for c in checks if c.status is ProbeStatus.FAIL],
            checks=checks,
            duration_s=(now_utc() - started).total_seconds(),
            transcript=result.stdout,
        )

    # -- local -------------------------------------------------------------- #
    def _local(self, node: NodeSpec) -> list[Check]:
        import platform
        import subprocess

        checks = [Check(name="reach", status=ProbeStatus.OK, detail="local process")]
        shell = shutil.which("bash")
        if shell:
            probe = subprocess.run(
                [shell, "-s"],
                input="if [ 1 -eq 1 ]; then\necho shell-if-ok\nfi\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            checks.append(
                Check(
                    name="shell",
                    status=ProbeStatus.OK if "shell-if-ok" in probe.stdout else ProbeStatus.FAIL,
                    detail=f"{shell}: {probe.stdout.strip() or probe.stderr.strip()[:120]}",
                )
            )
        else:
            checks.append(
                Check(
                    name="shell",
                    status=ProbeStatus.WARN,
                    detail="bash not found; remote-style scripts cannot run here",
                )
            )
        root = Path(self.config.home) / "runs"
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe_dir = root / f".rcf-selftest-{secrets.token_hex(4)}"
            probe_dir.mkdir()
            (probe_dir / "probe").write_text("probe", encoding="utf-8")
            ok = (probe_dir / "probe").read_text(encoding="utf-8") == "probe"
            shutil.rmtree(probe_dir, ignore_errors=True)
            checks.append(
                Check(
                    name="work_root",
                    status=ProbeStatus.OK if ok else ProbeStatus.FAIL,
                    detail=f"{probe_dir} writable and cleaned up",
                )
            )
        except OSError as exc:
            checks.append(
                Check(name="work_root", status=ProbeStatus.FAIL, detail=str(exc)[:200])
            )
        python = shutil.which("python3") or shutil.which("python")
        checks.append(
            Check(
                name="python",
                status=ProbeStatus.OK if python else ProbeStatus.WARN,
                detail=python or "python3 not on PATH",
            )
        )
        checks.append(
            Check(
                name="platform",
                status=ProbeStatus.OK,
                detail=f"{platform.system()} {platform.machine()} (local executor)",
            )
        )
        return checks


def _evaluate(
    sections: dict[str, str], *, stderr: str, returncode: int, expects_scheduler: bool
) -> list[Check]:
    checks: list[Check] = []
    reach = sections.get("reach", "").strip()
    checks.append(
        Check(
            name="connect",
            status=ProbeStatus.OK if "rcf-ok" in reach else ProbeStatus.FAIL,
            detail="ssh + command execution" if "rcf-ok" in reach else (stderr.strip()[:200] or "no output"),
        )
    )
    shell = sections.get("shell", "")
    shell_ok = "shell-if-ok" in shell and "shell-pipe-ok" in shell
    checks.append(
        Check(
            name="shell",
            status=ProbeStatus.OK if shell_ok else ProbeStatus.FAIL,
            detail="multi-line POSIX script + pipeline executed"
            if shell_ok
            else f"unexpected output: {shell.strip()[:160] or stderr.strip()[:160]}",
        )
    )
    work = sections.get("work", "")
    work_ok = "write-ok" in work and "read-ok" in work
    cleanup_ok = "cleanup=ok" in work
    checks.append(
        Check(
            name="work_root",
            status=ProbeStatus.OK if work_ok and cleanup_ok else ProbeStatus.FAIL,
            detail="create/write/read/cleanup under work_root"
            if work_ok and cleanup_ok
            else f"{work.strip()[:200] or 'no output'}",
        )
    )
    python = sections.get("python", "").strip()
    checks.append(
        Check(
            name="python",
            status=ProbeStatus.OK if "missing" not in python else ProbeStatus.WARN,
            detail=python[:120] or "not reported",
        )
    )
    gpu = sections.get("gpu", "").strip()
    if gpu:
        absent = gpu == "gpus=absent"
        checks.append(
            Check(
                name="gpu",
                status=(
                    ProbeStatus.SKIPPED
                    if absent and expects_scheduler
                    else ProbeStatus.WARN
                    if absent
                    else ProbeStatus.OK
                ),
                detail=(
                    "login node has no GPU; capacity is granted by the scheduler"
                    if absent and expects_scheduler
                    else "no GPU on this host"
                    if absent
                    else gpu.replace("gpus=", "") + " visible GPU(s)"
                ),
            )
        )
    slurm = sections.get("slurm", "").strip()
    if slurm:
        missing = "missing" in slurm
        checks.append(
            Check(
                name="scheduler",
                status=(
                    ProbeStatus.FAIL
                    if missing and expects_scheduler
                    else ProbeStatus.SKIPPED
                    if missing
                    else ProbeStatus.OK
                ),
                detail=(
                    "node is declared as slurm but sbatch is missing"
                    if missing and expects_scheduler
                    else "no scheduler (kind = ssh)"
                    if missing
                    else slurm.replace("slurm=", "")[:120]
                ),
            )
        )
    if returncode not in (0, None) and not [c for c in checks if c.status is ProbeStatus.FAIL]:
        checks.append(
            Check(
                name="exit_status",
                status=ProbeStatus.WARN,
                detail=f"self-test script exited {returncode}",
            )
        )
    return checks
