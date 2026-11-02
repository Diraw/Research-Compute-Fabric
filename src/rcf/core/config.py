"""Configuration loading: home directory, settings and declarative registries.

Layout (all optional, everything has a safe default)::

    $RCF_HOME/                 (default: ~/.rcf)
    ├── rcf.toml               settings + security policy
    ├── nodes.toml             [nodes.<id>] ...      (+ nodes.d/*.toml)
    ├── projects.toml          [projects.<id>] ...   (+ projects.d/*.toml)
    ├── environments.toml      [environments.<id>] ...
    └── state/rcf.db           SQLite (runs, plans, leases, audit)

Credentials are *never* part of this tree: nodes reference an ``ssh_alias``
resolved by the user's own ``~/.ssh/config`` and ssh-agent.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import Field, ValidationError

from ..core.errors import ConfigError, NotFound, SpecError
from ..core.models import EnvironmentSpec, NodeSpec, ProjectSpec, SpecModel
from ..security.policy import SecurityPolicy, normalize_ssh_options

SETTINGS_FILE = "rcf.toml"
NODES_FILE = "nodes.toml"
PROJECTS_FILE = "projects.toml"
ENVIRONMENTS_FILE = "environments.toml"
REGISTRY_DIRS = {
    NODES_FILE: "nodes",
    PROJECTS_FILE: "projects",
    ENVIRONMENTS_FILE: "environments",
}


class SecuritySettings(SpecModel):
    allow_execute: bool = True
    #: Deprecated: environment mutation is governed by [environments].managed_roots.
    #: Accepted so older configs keep loading; it no longer decides anything.
    allow_environment_mutation: bool | None = None
    allow_system_admin: bool = False
    require_confirmation: bool = True


class DatabaseSettings(SpecModel):
    path: str = "state/rcf.db"


class EnvironmentSettings(SpecModel):
    """Where RCF may create or modify Python environments.

    Empty by default: RCF cannot touch any environment until you name a
    directory that belongs to you.  See ``environments/ownership.py`` for the
    exact rules.
    """

    managed_roots: list[str] = []


class RunSettings(SpecModel):
    """How long a plan stays valid.

    A plan is a claim about the node ("this much CPU is free, this GPU is idle").
    Claims age, so submitting a stale plan is refused instead of launching a job
    against a picture of the machine that may no longer be true.
    """

    plan_ttl_minutes: int = 30


class DefaultsSettings(SpecModel):
    probe_timeout: int = 45
    connect_timeout: int = 10
    log_tail: int = 200
    max_log_bytes: int = 2_000_000
    gpu_lease_ttl_minutes: int = 720
    inspect_cache_seconds: int = 60


class StorageSettings(SpecModel):
    """Bounded, explicit scans; budgets warn, never authorize deletion."""

    max_entries: int = 20000
    max_scan_seconds: int = 30
    plan_ttl_minutes: int = 30
    stale_scan_hours: int = 24
    keep_recent_runs: int = 5


class ProgressSettings(SpecModel):
    enforce_long_runs: bool = True
    short_run_minutes: int = Field(default=5, ge=1, le=60)
    calibration_max_age_hours: int = Field(default=168, ge=1, le=8760)
    max_wait_seconds: int = Field(default=60, ge=1, le=300)
    collect_timeout_seconds: int = Field(default=5, ge=1, le=30)


class Settings(SpecModel):
    security: SecuritySettings = SecuritySettings()
    database: DatabaseSettings = DatabaseSettings()
    defaults: DefaultsSettings = DefaultsSettings()
    environments: EnvironmentSettings = EnvironmentSettings()
    runs: RunSettings = RunSettings()
    storage: StorageSettings = StorageSettings()
    progress: ProgressSettings = ProgressSettings()


class NodeRegistryFile(SpecModel):
    nodes: dict[str, NodeSpec] = {}


class ProjectRegistryFile(SpecModel):
    projects: dict[str, ProjectSpec] = {}


class EnvironmentRegistryFile(SpecModel):
    environments: dict[str, EnvironmentSpec] = {}


def default_home() -> Path:
    raw = os.environ.get("RCF_HOME", "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return (Path.home() / ".rcf").resolve()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}", detail={"path": str(path)}) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}", detail={"path": str(path)}) from exc


def _merge_registry_files(base: Path, primary: str) -> list[Path]:
    """``xxx.toml`` plus every ``xxx.d/*.toml`` (sorted, deterministic)."""

    files: list[Path] = []
    main = base / primary
    if main.is_file():
        files.append(main)
    directory = base / f"{Path(primary).stem}.d"
    if directory.is_dir():
        files.extend(sorted(p for p in directory.glob("*.toml") if p.is_file()))
    return files


ModelT = TypeVar("ModelT", NodeSpec, ProjectSpec, EnvironmentSpec)


def _load_registry(
    *,
    home: Path,
    filename: str,
    table: str,
    model: type[ModelT],
) -> tuple[dict[str, ModelT], list[Path]]:
    entries: dict[str, ModelT] = {}
    files = _merge_registry_files(home, filename)
    for path in files:
        raw = _read_toml(path)
        section = raw.get(table, {})
        if not isinstance(section, dict):
            raise SpecError(f"{path.name}: [{table}] must be a table", detail={"path": str(path)})
        unknown_top = set(raw) - {table}
        if unknown_top:
            raise SpecError(
                f"{path.name}: unknown top-level key(s) {sorted(unknown_top)}; expected only [{table}]",
                detail={"path": str(path)},
            )
        for key, payload in section.items():
            if not isinstance(payload, dict):
                raise SpecError(
                    f"{path.name}: [{table}.{key}] must be a table",
                    detail={"path": str(path), "id": key},
                )
            candidate = dict(payload)
            candidate.setdefault("id", key)
            if candidate["id"] != key:
                raise SpecError(
                    f"{path.name}: entry key {key!r} does not match its id {candidate['id']!r}",
                    detail={"path": str(path), "key": key},
                )
            if key in entries:
                raise SpecError(
                    f"duplicate {table[:-1]} id {key!r} (also defined in another registry file)",
                    detail={"path": str(path), "id": key},
                )
            try:
                entries[key] = model.model_validate(candidate)
            except ValidationError as exc:
                raise SpecError(
                    f"{path.name}: invalid [{table}.{key}]\n{_format_validation(exc)}",
                    detail={"path": str(path), "id": key, "errors": exc.errors(include_url=False)},
                ) from exc
    return entries, files


def _format_validation(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


class Config:
    """Fully validated, read-only view of a RCF home directory."""

    def __init__(
        self,
        *,
        home: Path,
        settings: Settings,
        nodes: dict[str, NodeSpec],
        projects: dict[str, ProjectSpec],
        environments: dict[str, EnvironmentSpec],
        loaded_files: list[Path],
        warnings: list[str],
    ) -> None:
        self.home = home
        self.settings = settings
        self.nodes = nodes
        self.projects = projects
        self.environments = environments
        self.loaded_files = loaded_files
        self.warnings = warnings

    # -- construction ------------------------------------------------------ #
    @classmethod
    def load(cls, home: Path | str | None = None) -> Config:
        base = Path(home).resolve() if home else default_home()
        settings = Settings()
        loaded: list[Path] = []
        settings_path = base / SETTINGS_FILE
        if settings_path.is_file():
            raw = _read_toml(settings_path)
            unknown = set(raw) - {"security", "database", "defaults", "environments", "runs", "storage", "progress", "rcf"}
            if unknown:
                raise SpecError(
                    f"{settings_path.name}: unknown section(s) {sorted(unknown)}",
                    detail={"path": str(settings_path)},
                )
            raw.pop("rcf", None)
            try:
                settings = Settings.model_validate(raw)
            except ValidationError as exc:
                raise SpecError(
                    f"{settings_path.name} is invalid\n{_format_validation(exc)}",
                    detail={"path": str(settings_path)},
                ) from exc
            loaded.append(settings_path)

        nodes, node_files = _load_registry(
            home=base, filename=NODES_FILE, table="nodes", model=NodeSpec
        )
        projects, project_files = _load_registry(
            home=base, filename=PROJECTS_FILE, table="projects", model=ProjectSpec
        )
        environments, env_files = _load_registry(
            home=base, filename=ENVIRONMENTS_FILE, table="environments", model=EnvironmentSpec
        )
        loaded.extend(node_files + project_files + env_files)

        warnings: list[str] = []
        # Security invariants on ssh options are validated at load time.
        for node in nodes.values():
            if node.ssh.extra_options:
                node.ssh.extra_options = normalize_ssh_options(list(node.ssh.extra_options))

        _validate_cross_references(nodes, projects, environments)

        if not base.exists():
            warnings.append(
                f"RCF home {base} does not exist yet; run `rcf init` to create a starter layout"
            )
        return cls(
            home=base,
            settings=settings,
            nodes=nodes,
            projects=projects,
            environments=environments,
            loaded_files=loaded,
            warnings=warnings,
        )

    # -- accessors --------------------------------------------------------- #
    @property
    def db_path(self) -> Path:
        candidate = Path(os.path.expandvars(os.path.expanduser(self.settings.database.path)))
        return candidate if candidate.is_absolute() else (self.home / candidate)

    @property
    def managed_roots(self) -> list[str]:
        return list(self.settings.environments.managed_roots)

    def policy(self) -> SecurityPolicy:
        return SecurityPolicy(
            allow_execute=self.settings.security.allow_execute,
            allow_environment_mutation=bool(self.settings.security.allow_environment_mutation),
            allow_system_admin=self.settings.security.allow_system_admin,
            require_confirmation=self.settings.security.require_confirmation,
        )

    def node(self, node_id: str) -> NodeSpec:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise NotFound(
                f"unknown node {node_id!r}",
                hint=f"known nodes: {', '.join(sorted(self.nodes)) or '<none>'}",
                detail={"node_id": node_id},
            ) from None

    def project(self, project_id: str) -> ProjectSpec:
        try:
            return self.projects[project_id]
        except KeyError:
            raise NotFound(
                f"unknown project {project_id!r}",
                hint=f"known projects: {', '.join(sorted(self.projects)) or '<none>'}",
                detail={"project_id": project_id},
            ) from None

    def environment(self, env_id: str) -> EnvironmentSpec:
        try:
            return self.environments[env_id]
        except KeyError:
            raise NotFound(
                f"unknown environment {env_id!r}",
                hint=f"known environments: {', '.join(sorted(self.environments)) or '<none>'}",
                detail={"env_id": env_id},
            ) from None

    def nodes_for_labels(self, required: dict[str, str]) -> list[NodeSpec]:
        return [
            node
            for node in self.nodes.values()
            if all(node.labels.get(key) == value for key, value in required.items())
        ]

    def ensure_dirs(self) -> None:
        (self.home / "state").mkdir(parents=True, exist_ok=True)
        (self.home / "logs").mkdir(parents=True, exist_ok=True)


def _validate_cross_references(
    nodes: dict[str, NodeSpec],
    projects: dict[str, ProjectSpec],
    environments: dict[str, EnvironmentSpec],
) -> None:
    known = set(nodes)
    for env in environments.values():
        for node_id in env.path:
            if node_id != "*" and node_id not in known:
                raise SpecError(
                    f"environment {env.id!r} references unknown node {node_id!r}",
                    hint=f"known nodes: {', '.join(sorted(known)) or '<none>'}",
                    detail={"env_id": env.id, "node_id": node_id},
                )
    for project in projects.values():
        for node_id in list(project.paths) + list(project.data_paths):
            if node_id != "*" and node_id not in known:
                raise SpecError(
                    f"project {project.id!r} references unknown node {node_id!r}",
                    hint=f"known nodes: {', '.join(sorted(known)) or '<none>'}",
                    detail={"project_id": project.id, "node_id": node_id},
                )
        if project.env_id and project.env_id not in environments:
            raise SpecError(
                f"project {project.id!r} references unknown environment {project.env_id!r}",
                detail={"project_id": project.id, "env_id": project.env_id},
            )


STARTER_RCF_TOML = """\
# RCF settings. Everything here is optional.
[security]
allow_execute = true
# Environment writes require configured managed_roots; system administration stays off.
# Legacy compatibility field; managed_roots controls environment writes.
allow_environment_mutation = false
allow_system_admin = false
require_confirmation = true

[database]
path = "state/rcf.db"

[defaults]
probe_timeout = 45
log_tail = 200
"""

STARTER_NODES_TOML = """\
# Compute nodes. Credentials never live here: `ssh_alias` points at a host in
# your ~/.ssh/config, and keys are handled by ssh-agent.
#
# [nodes.cluster]
# kind = "slurm"
# ssh_alias = "cluster"
# work_root = "/home/<you>/rcf"
# labels = { site = "lab", gpu = "yes" }
#
# [nodes.cluster.slurm]
# default_partition = "gpu"
# partitions = ["gpu", "cpu"]
#
# [nodes.workstation]
# kind = "ssh"
# ssh_alias = "workstation"
# work_root = "/home/<you>/rcf"
# labels = { site = "office", gpu = "yes" }
#
# [nodes.this-laptop]
# kind = "local"
# labels = { site = "laptop", gpu = "yes" }
"""

STARTER_PROJECTS_TOML = """\
# Research projects. One logical id, one real directory per node.
#
# [projects.simulation-demo]
# git_url = "git@example.com:me/simulation-demo.git"
# entrypoint = "{python} -m simulation_demo.train --config {params_file} --out {output_dir}"
# script = "scripts/train.py"
# env_id = "science"
# outputs = "outputs"
# 产物契约（默认 argument）：entrypoint 必须把 {output_dir} 传给脚本。
# 脚本自己读 $RCF_OUTPUT_DIR 时写 "env"；改不动的老脚本写 "none"（只告警）。
# output_contract = "argument"
# paths = { "cluster" = "/home/<you>/proj/simulation-demo", "*" = "~/proj/simulation-demo" }
"""

STARTER_ENVIRONMENTS_TOML = """\
# Environment requirements. RCF inspects them read-only and can plan an install;
# it never mutates a system interpreter.
#
# [environments.science]
# kind = "venv"
# python = "python3"
# path = { "cluster" = "/home/<you>/envs/science", "*" = "~/.venvs/science" }
# packages = [{ name = "numpy", version = ">=1.26" }, { name = "torch" }]
# verify_command = "{python} -c 'import numpy, torch; print(numpy.__version__, torch.__version__)'"
"""


def write_starter_home(home: Path, *, overwrite: bool = False) -> list[Path]:
    """Create a commented starter layout; never overwrites unless asked."""

    home.mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(exist_ok=True)
    created: list[Path] = []
    for name, content in (
        (SETTINGS_FILE, STARTER_RCF_TOML),
        (NODES_FILE, STARTER_NODES_TOML),
        (PROJECTS_FILE, STARTER_PROJECTS_TOML),
        (ENVIRONMENTS_FILE, STARTER_ENVIRONMENTS_TOML),
    ):
        path = home / name
        if path.exists() and not overwrite:
            continue
        path.write_text(content, encoding="utf-8")
        created.append(path)
    return created


def iter_files(config: Config) -> Iterable[Path]:  # pragma: no cover - convenience
    return iter(config.loaded_files)


def as_jsonable(value: Any) -> Any:  # pragma: no cover - small helper
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value
