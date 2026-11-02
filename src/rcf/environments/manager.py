"""Environment inspection and install *planning*.

Two hard rules shape this module:

1. Inspection is read-only.  RCF runs one short Python script inside the target
   interpreter to report its version and the installed distributions; it never
   imports the scientific stack itself.
2. RCF never mutates a system interpreter or a module tree.  Install steps are
   only generated for *environment-scoped* locations (a venv/conda prefix),
   never with ``sudo``, and executing them needs an explicit opt-in plus
   confirmation.

The three states from the brief are kept distinct:
``installed`` (the interpreter exists) → ``compatible`` (requirements match)
→ ``verified`` (a project supplied command actually ran successfully).
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Sequence

from ..core.config import Config
from ..core.errors import NotFound, PermissionDenied, RcfError, UnsupportedOperation
from ..core.ids import now_utc, stable_hash
from ..core.models import (
    Check,
    EnvironmentKind,
    EnvironmentPlan,
    EnvironmentSpec,
    EnvironmentState,
    EnvironmentStep,
    EnvironmentView,
    NodeKind,
    NodeSpec,
    ProbeStatus,
)
from ..core.runner import CommandResult, Runner, join_command, shquote
from ..core.versions import satisfies
from ..executors.ssh import SshTransport
from ..security.policy import SecurityPolicy
from ..security.redaction import redact
from .ownership import PrefixVerdict, check_prefix

PROBE_SCRIPT = r'''
import json, sys
names = %(names)s
out = {"python": "%%d.%%d.%%d" %% sys.version_info[:3],
       "executable": sys.executable,
       "platform": sys.platform,
       "packages": {}}
for name in names:
    version = None
    try:
        from importlib import metadata as _md
        version = _md.version(name)
    except Exception:
        try:
            import pkg_resources
            version = pkg_resources.get_distribution(name).version
        except Exception:
            version = None
    out["packages"][name] = version

# Fingerprint the whole installed set, not only the declared packages: a CUDA
# runtime or BLAS bump changes results while "torch==2.2" stays the same.
installed = []
try:
    from importlib import metadata as _allmd
    for _dist in _allmd.distributions():
        _name = _dist.metadata["Name"] if _dist.metadata else None
        if _name:
            installed.append(_name + "==" + str(_dist.version))
except Exception:
    try:
        import pkg_resources as _pkg
        for _dist in _pkg.working_set:
            installed.append(_dist.project_name + "==" + str(_dist.version))
    except Exception:
        installed = []
installed.sort()
out["dist_count"] = len(installed)
import hashlib as _hashlib
out["env_digest"] = _hashlib.sha256(chr(10).join(installed).encode("utf-8")).hexdigest()[:16]
print(json.dumps(out))
'''

MODULE_INIT = (
    "source /etc/profile.d/modules.sh 2>/dev/null || "
    "source /usr/share/Modules/init/bash 2>/dev/null || true"
)


def normalize_package(name: str) -> str:
    return name.strip().lower().replace("_", "-")


class EnvironmentManager:
    def __init__(self, config: Config, runner: Runner, *, store=None) -> None:
        self.config = config
        self.runner = runner
        self.store = store
        self.policy: SecurityPolicy = config.policy()
        from ..core.nodeops import NodeOps

        self.ops = NodeOps(runner)

    # -- ownership ---------------------------------------------------------- #
    def _ownership(self, env: EnvironmentSpec, node: NodeSpec, path: str) -> PrefixVerdict:
        """Decide whether RCF may write into ``path`` on this node."""

        home = self.ops.home_dir(node)
        exists = self.ops.dir_exists(node, path) is ProbeStatus.OK
        known = bool(
            self.store is not None
            and self.store.is_managed_prefix(node_id=node.id, path=path)
        )
        verdict = check_prefix(
            path=path,
            home=home,
            managed_roots=self.config.managed_roots,
            exists=exists,
            known_to_rcf=known,
            declared_managed=env.managed,
        )
        if verdict.blocked:
            return verdict
        inspection = self._inspect_path(node, path)
        if inspection.blocked:
            return inspection.verdict
        # A failed existence probe must not silently authorize overwriting an
        # existing prefix: repeat ownership with the target-side evidence.
        return check_prefix(
            path=path, home=home, managed_roots=self.config.managed_roots,
            exists=inspection.exists, known_to_rcf=known, declared_managed=env.managed,
        )

    def _inspect_path(self, node: NodeSpec, path: str):
        from .path_guard import inspect_prefix_path

        return inspect_prefix_path(node, path, self.runner)

    # -- paths / commands --------------------------------------------------- #
    @staticmethod
    def interpreter(env: EnvironmentSpec, node: NodeSpec) -> list[str] | None:
        path = env.path_for(node.id)
        python = env.python or "python3"
        if env.kind in (EnvironmentKind.VENV, EnvironmentKind.CONDA):
            if not path:
                return None
            # Windows virtualenvs live in Scripts/python.exe; POSIX in bin/python3.
            if os.name == "nt" and node.kind is NodeKind.LOCAL:
                executable = python if python.lower().endswith(".exe") else f"{python}.exe"
                return [f"{path.rstrip('/')}/Scripts/{executable}"]
            return [f"{path.rstrip('/')}/bin/{python}"]
        if env.kind is EnvironmentKind.APPTAINER:
            if not env.apptainer_image:
                return None
            return ["apptainer", "exec", env.apptainer_image, python]
        return [python]

    @staticmethod
    def preamble(env: EnvironmentSpec, node: NodeSpec | None = None) -> list[str]:
        """Shell lines that prepare the environment before the command runs.

        A conda environment is activated **by prefix**, not by the RCF
        environment id: the registry id need not match the installed prefix.
        Activating an unrelated name could silently select the wrong runtime.
        """

        lines: list[str] = []
        if env.modules:
            lines.append(MODULE_INIT)
            lines.extend(f"module load {module}" for module in env.modules)
        if env.kind is EnvironmentKind.CONDA and env.conda_root:
            lines.append(
                f"source {env.conda_root.rstrip('/')}/etc/profile.d/conda.sh 2>/dev/null || true"
            )
            prefix = env.path_for(node.id) if node is not None else None
            target = shquote(prefix) if prefix else env.id
            lines.append(f"conda activate {target} 2>/dev/null || true")
        return lines

    # -- inspection --------------------------------------------------------- #
    def inspect(
        self, env: EnvironmentSpec, node: NodeSpec, *, verify: bool = False, confirmed: bool = False,
    ) -> EnvironmentView:
        self.policy.check("environment.inspect")
        if verify:
            self.policy.check("environment.verify")
            # Arbitrary verification code always needs approval for this call,
            # even when ordinary execution confirmation is disabled globally.
            if not confirmed:
                raise PermissionDenied("environment.verify requires explicit confirmation")
        checks: list[Check] = []
        notes: list[str] = []
        interpreter = self.interpreter(env, node)
        path = env.path_for(node.id)
        if interpreter is None:
            return EnvironmentView(
                env_id=env.id,
                node_id=node.id,
                kind=env.kind,
                state=EnvironmentState.MISSING,
                path=path,
                checks=[
                    Check(
                        name="environment",
                        status=ProbeStatus.FAIL,
                        detail=f"no path configured for node {node.id!r} and kind {env.kind.value}",
                    )
                ],
                notes=["define environments.toml [env.path] for this node"],
            )

        probe = self._run_python(env, node, interpreter, PROBE_SCRIPT % {"names": json.dumps([p.name for p in env.packages])})
        if probe is None or probe.returncode != 0:
            detail = (
                f"interpreter not usable: {redact(probe.first_error_line())}"
                if probe is not None
                else "interpreter not usable"
            )
            checks.append(Check(name="interpreter", status=ProbeStatus.FAIL, detail=detail))
            return EnvironmentView(
                env_id=env.id,
                node_id=node.id,
                kind=env.kind,
                state=EnvironmentState.MISSING,
                path=path,
                checks=checks,
                notes=notes,
            )

        try:
            payload = json.loads(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            checks.append(
                Check(
                    name="interpreter",
                    status=ProbeStatus.FAIL,
                    detail=f"unexpected probe output: {redact(probe.stdout)[:200]}",
                )
            )
            return EnvironmentView(
                env_id=env.id,
                node_id=node.id,
                kind=env.kind,
                state=EnvironmentState.MISSING,
                path=path,
                checks=checks,
            )

        python_version = payload.get("python")
        env_digest = payload.get("env_digest")
        dist_count = payload.get("dist_count")
        installed: dict[str, str | None] = {
            normalize_package(k): v for k, v in (payload.get("packages") or {}).items()
        }
        checks.append(
            Check(name="interpreter", status=ProbeStatus.OK, detail=f"python {python_version} at {path or payload.get('executable')}")
        )

        compatible = True
        if env.python_version and not satisfies(python_version, env.python_version):
            compatible = False
            checks.append(
                Check(
                    name="python_version",
                    status=ProbeStatus.FAIL,
                    detail=f"python {python_version} does not satisfy {env.python_version}",
                )
            )
        missing: list[str] = []
        for requirement in env.packages:
            key = normalize_package(requirement.name)
            found = installed.get(key)
            if found is None:
                if requirement.optional:
                    checks.append(
                        Check(name=f"pkg:{key}", status=ProbeStatus.WARN, detail="optional, not installed")
                    )
                else:
                    missing.append(requirement.name)
                    compatible = False
                    checks.append(
                        Check(name=f"pkg:{key}", status=ProbeStatus.FAIL, detail="not installed")
                    )
                continue
            try:
                ok = satisfies(found, requirement.version)
            except ValueError as exc:
                ok = False
                notes.append(str(exc))
            if not ok:
                compatible = False
                checks.append(
                    Check(
                        name=f"pkg:{key}",
                        status=ProbeStatus.FAIL,
                        detail=f"{found} does not satisfy {requirement.version}",
                    )
                )
            else:
                checks.append(Check(name=f"pkg:{key}", status=ProbeStatus.OK, detail=found or "installed"))

        fingerprint = stable_hash(
            {
                "env_id": env.id,
                "node_id": node.id,
                "kind": env.kind.value,
                "path": path,
                "python": python_version,
                "env_digest": env_digest,
                "packages": installed,
                "requirements": [p.model_dump() for p in env.packages],
                "python_version_requirement": env.python_version,
            }
        )
        state = EnvironmentState.COMPATIBLE if compatible else EnvironmentState.INSTALLED
        verified_at = None
        if verify and compatible and env.verify_command:
            verified_at = self._verify(env, node, interpreter, fingerprint)
            if verified_at is not None:
                state = EnvironmentState.VERIFIED
        elif compatible and self.store is not None:
            verified_at = self.store.get_verification(env_id=env.id, node_id=node.id, fingerprint=fingerprint)
            if verified_at is not None:
                state = EnvironmentState.VERIFIED

        return EnvironmentView(
            env_id=env.id,
            node_id=node.id,
            kind=env.kind,
            state=state,
            path=path,
            python_version=python_version,
            fingerprint=fingerprint,
            env_digest=env_digest,
            dist_count=dist_count,
            packages=installed,
            missing_packages=missing,
            checks=checks,
            verified_at=verified_at,
            notes=notes,
        )

    def _verify(self, env: EnvironmentSpec, node: NodeSpec, interpreter: Sequence[str], fingerprint: str):
        if not env.verify_command:
            return None
        command = env.verify_command.replace("{python}", " ".join(interpreter))
        result = self._run_shell(env, node, command, timeout=300)
        if result is None or result.returncode != 0:
            return None
        stamp = now_utc()
        if self.store is not None:
            self.store.record_verification(
                env_id=env.id,
                node_id=node.id,
                fingerprint=fingerprint,
                detail={"output": redact(result.stdout)[-800:], "command": command},
            )
        return stamp

    # -- planning ----------------------------------------------------------- #
    def plan(self, env: EnvironmentSpec, node: NodeSpec, *, view: EnvironmentView | None = None) -> EnvironmentPlan:
        view = view or self.inspect(env, node)
        if view.state is EnvironmentState.MISSING:
            # "not there yet" is what this plan is about to fix; showing it as a
            # failure next to "will be created" reads like a contradiction.
            view = view.model_copy(
                update={
                    "checks": [
                        c
                        for c in view.checks
                        if not (c.name == "interpreter" and c.status is ProbeStatus.FAIL)
                    ]
                }
            )
        checks: list[Check] = []
        blockers: list[str] = []
        steps: list[EnvironmentStep] = []
        notes: list[str] = []
        path = env.path_for(node.id)

        if view.state is EnvironmentState.VERIFIED or view.state is EnvironmentState.COMPATIBLE:
            return EnvironmentPlan(
                env_id=env.id,
                node_id=node.id,
                action="none",
                checks=[Check(name="environment", status=ProbeStatus.OK, detail=f"state={view.state.value}")],
                requires_authorization=False,
                notes=["nothing to do"],
            )

        if env.kind in (EnvironmentKind.SYSTEM, EnvironmentKind.MODULE, EnvironmentKind.APPTAINER):
            blockers.append(
                f"{env.kind.value} environments are inspected only: RCF never modifies a system "
                "interpreter, module tree or container image"
            )
            return EnvironmentPlan(
                env_id=env.id,
                node_id=node.id,
                action="blocked",
                checks=view.checks,
                blockers=blockers,
                notes=notes,
            )

        if not path:
            blockers.append(f"no path configured for node {node.id!r}")
            return EnvironmentPlan(
                env_id=env.id, node_id=node.id, action="blocked", checks=view.checks, blockers=blockers
            )

        verdict = self._ownership(env, node, path)
        if verdict.blocked:
            return EnvironmentPlan(
                env_id=env.id,
                node_id=node.id,
                action="blocked",
                checks=view.checks,
                blockers=[verdict.reason],
                notes=notes,
            )
        python = env.python or "python3"
        python_bin = f"{path.rstrip('/')}/bin/{python}"
        declared = [f"{p.name}{p.version or ''}" for p in env.packages if not p.optional]

        if env.kind is EnvironmentKind.CONDA:
            missing_from_collection = [
                f"{p.name}{p.version or ''}"
                for p in env.packages
                if not p.optional and p.name in view.missing_packages
            ]
            conda_bin = (
                f"{env.conda_root.rstrip('/')}/bin/conda" if env.conda_root else "conda"
            )
            if view.state is EnvironmentState.MISSING:
                if not env.conda_python:
                    blockers.append(
                        "creating a conda environment needs an exact version: set "
                        'conda_python = "3.12" on this environment'
                    )
                    return EnvironmentPlan(
                        env_id=env.id,
                        node_id=node.id,
                        action="blocked",
                        checks=view.checks,
                        blockers=blockers,
                        notes=notes,
                    )
                steps.append(
                    EnvironmentStep(
                        description=f"create conda environment at {path}",
                        command=[
                            conda_bin,
                            "create",
                            "-y",
                            "-p",
                            path,
                            f"python={env.conda_python}",
                            *declared,
                        ],
                        requires_network=True,
                    )
                )
                checks.append(
                    Check(name="interpreter", status=ProbeStatus.WARN, detail="will be created")
                )
            elif missing_from_collection:
                steps.append(
                    EnvironmentStep(
                        description=f"install {len(missing_from_collection)} package(s) with conda",
                        command=[conda_bin, "install", "-y", "-p", path, *missing_from_collection],
                        requires_network=True,
                    )
                )
            if env.verify_command:
                steps.append(
                    EnvironmentStep(
                        description="verify the environment with the project command",
                        command=[
                            "bash",
                            "-lc",
                            env.verify_command.replace("{python}", python_bin),
                        ],
                        mutating=False,
                        requires_network=False,
                    )
                )
            return EnvironmentPlan(
                env_id=env.id,
                node_id=node.id,
                action="create" if view.state is EnvironmentState.MISSING else "install",
                steps=steps,
                checks=view.checks + checks,
                blockers=blockers if not steps else [],
                requires_authorization=True,
                notes=[*notes, verdict.reason],
            )

        uv_available = self._has_command(env, node, "uv")
        pip_installer = (
            ["uv", "pip", "install", "--python", python_bin]
            if uv_available
            else [python_bin, "-m", "pip", "install"]
        )
        if not uv_available:
            notes.append("uv not found on the node; falling back to pip inside the target environment")

        if view.state is EnvironmentState.MISSING:
            base_python = env.base_python or env.python or "python3"
            if uv_available:
                steps.append(
                    EnvironmentStep(
                        description=f"create virtual environment at {path}",
                        command=["uv", "venv", "--python", base_python, path],
                        requires_network=False,
                    )
                )
            else:
                steps.append(
                    EnvironmentStep(
                        description=f"create virtual environment at {path}",
                        command=[base_python, "-m", "venv", path],
                        requires_network=False,
                    )
                )
            checks.append(Check(name="interpreter", status=ProbeStatus.WARN, detail="will be created"))

        packages = declared
        if packages:
            steps.append(
                EnvironmentStep(
                    description=f"install {len(packages)} package(s) into {path}",
                    command=[*pip_installer, *env.install_args, *packages],
                    requires_network=True,
                )
            )
        if not steps:
            blockers.append("no actionable step could be derived from the current environment state")
            return EnvironmentPlan(
                env_id=env.id,
                node_id=node.id,
                action="blocked",
                checks=view.checks,
                blockers=blockers,
                notes=notes,
            )

        if env.verify_command:
            steps.append(
                EnvironmentStep(
                    description="verify the environment with the project command",
                    command=[
                        "bash",
                        "-lc",
                        env.verify_command.replace("{python}", python_bin),
                    ],
                    mutating=False,
                    requires_network=False,
                )
            )
        return EnvironmentPlan(
            env_id=env.id,
            node_id=node.id,
            action="create" if view.state is EnvironmentState.MISSING else "install",
            steps=steps,
            checks=view.checks + checks,
            blockers=blockers,
            requires_authorization=True,
            notes=[*notes, verdict.reason],
        )

    def apply(
        self,
        plan: EnvironmentPlan,
        env: EnvironmentSpec,
        node: NodeSpec,
        *,
        confirmed: bool,
    ) -> list[dict[str, object]]:
        self.policy.check("environment.apply")
        self.policy.check_confirmation("environment.apply", confirmed)
        if plan.blockers:
            raise PermissionDenied(
                "environment plan has blockers and cannot be applied",
                detail={"blockers": plan.blockers},
            )
        path = env.path_for(node.id) or ""
        verdict = self._ownership(env, node, path)
        if verdict.blocked:
            raise PermissionDenied(
                f"RCF may not modify {path} on {node.id!r}: {verdict.reason}",
                hint="declare the directory in [environments].managed_roots, or set managed = true",
                detail={"env_id": env.id, "node_id": node.id, "path": path},
            )
        results: list[dict[str, object]] = []
        for step in plan.steps:
            # Reinspect ancestors before each write step. This cannot fence an
            # unrelated filesystem writer, but never trusts stale plan evidence.
            inspection = self._inspect_path(node, path)
            if inspection.blocked:
                raise PermissionDenied(inspection.reason, detail={"env_id": env.id, "path": path})
            result = self._run_argv(env, node, step.command, timeout=1800)
            record = {
                "description": step.description,
                "command": list(step.command),
                "returncode": None if result is None else result.returncode,
                "output": redact((result.stdout + result.stderr) if result else "")[-800:],
            }
            results.append(record)
            if result is None or result.returncode != 0:
                break
        # Record ownership as soon as the prefix exists, even when the install
        # step failed afterwards — otherwise a retry would be refused as an
        # "environment RCF did not create".
        created = False
        for step, result in zip(plan.steps, results, strict=False):
            command_text = " ".join(step.command)
            if result["returncode"] == 0 and (
                "venv" in command_text or ("conda" in command_text and "create" in command_text)
            ):
                created = True
                break
        if created and self.store is not None:
            self.store.record_managed_prefix(node_id=node.id, path=path, env_id=env.id)
        return results

    # -- execution helpers -------------------------------------------------- #
    # -- lock --------------------------------------------------------------- #
    def lock(self, env: EnvironmentSpec, node: NodeSpec) -> str:
        """Return a rebuildable manifest for this environment on this node.

        The fingerprint recorded with a run proves the environment did not
        change; the lock says *what* it contained, so the same environment can be
        rebuilt elsewhere when a result has to be reproduced.
        """

        lines = [
            f"# environment: {env.id}",
            f"# node: {node.id}",
            f"# path: {env.path_for(node.id) or '-'}",
            f"# kind: {env.kind.value}",
            f"# python: {env.python or 'python3'}",
        ]
        if env.conda_python:
            lines.append(f"# conda_python: {env.conda_python}")
        lines.append(f"# generated: {now_utc().isoformat(timespec='seconds')}")
        lines.append("")

        prefix = env.path_for(node.id)
        explicit = None
        if env.kind is EnvironmentKind.CONDA:
            explicit = self._run_shell(
                env, node, f"conda list --explicit -p {shquote(prefix)} 2>/dev/null", timeout=120
            )
        if explicit is not None and explicit.ok and "http" in explicit.stdout:
            lines.append("# conda list --explicit (recreate with: conda create -p <path> --file <this>)")
            lines.extend(explicit.stdout.strip().splitlines())
        else:
            interpreter = self.interpreter(env, node)
            if interpreter is None:
                raise NotFound(f"environment {env.id!r} has no path on node {node.id!r}")
            frozen = self._run_shell(
                env, node, f"{' '.join(shlex.quote(p) for p in interpreter)} -m pip freeze 2>/dev/null",
                timeout=120,
            )
            if frozen is None or not frozen.ok:
                raise UnsupportedOperation(
                    f"cannot read a package list for {env.id!r} on {node.id!r}",
                    hint="the interpreter may not have pip; install it or use a conda environment",
                )
            lines.append("# pip freeze (recreate with: pip install -r <this>)")
            lines.extend(frozen.stdout.strip().splitlines() or ["# (no packages installed by pip)"])

        lines.append("")
        try:
            view = self.inspect(env, node)
            lines.append(f"# fingerprint: {view.fingerprint}")
            lines.append(f"# state: {view.state.value}")
        except RcfError:  # a lock is still useful when inspection fails
            pass
        return "\n".join(lines) + "\n"

    def _run_python(
        self, env: EnvironmentSpec, node: NodeSpec, interpreter: Sequence[str], script: str
    ) -> CommandResult | None:
        return self._run_argv(env, node, [*interpreter, "-c", script], timeout=120)

    def _run_argv(
        self, env: EnvironmentSpec, node: NodeSpec, argv: Sequence[str], *, timeout: float
    ) -> CommandResult | None:
        preamble = self.preamble(env, node)
        preamble += [f"export {key}={shquote(value)}" for key, value in sorted(env.env_vars.items())]
        if node.kind is NodeKind.LOCAL:
            if preamble:
                return self.runner.run(["bash", "-lc", "\n".join(preamble) + "\n" + join_command(argv)], timeout=timeout)
            return self.runner.run(list(argv), timeout=timeout)
        transport = SshTransport.from_node(node, self.runner)
        command = "\n".join([*preamble, join_command(argv)]) if preamble else join_command(argv)
        return transport.run(command, timeout=timeout)

    def _run_shell(self, env: EnvironmentSpec, node: NodeSpec, command: str, *, timeout: float) -> CommandResult | None:
        preamble = self.preamble(env, node)
        preamble += [f"export {key}={shquote(value)}" for key, value in sorted(env.env_vars.items())]
        full = "\n".join([*preamble, command]) if preamble else command
        if node.kind is NodeKind.LOCAL:
            return self.runner.run(["bash", "-lc", full], timeout=timeout)
        return SshTransport.from_node(node, self.runner).run(full, timeout=timeout)

    def has_command(self, env: EnvironmentSpec, node: NodeSpec, name: str) -> bool:
        """Public wrapper: is ``name`` reachable once the environment is active?"""

        return self._has_command(env, node, name)

    def _has_command(self, env: EnvironmentSpec, node: NodeSpec, name: str) -> bool:
        try:
            result = self._run_shell(
                env, node, f"command -v {name} >/dev/null 2>&1 && echo yes || echo no", timeout=30
            )
        except RcfError:
            # No shell available (e.g. a Windows local node without bash): treat
            # the tool as absent instead of failing the whole plan.
            return False
        return bool(result and result.stdout.strip().endswith("yes"))
