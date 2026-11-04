"""Declarative specs and runtime data models.

The split matters:

* ``*Spec`` models are *declarative configuration* loaded from TOML.  They are
  validated strictly (``extra="forbid"``) so a typo in a config file fails loudly
  instead of being silently ignored.
* Runtime models (``ResourceSnapshot``, ``RunPlan``, ``RunRecord`` ...) are
  produced by RCF and are serialised to JSON/SQLite, therefore they tolerate
  additive fields (``extra="ignore"``) so that an older reader can still open a
  newer database.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ..progress.models import ProgressContract

NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
RESERVED_RUN_PARAMETERS = frozenset({
    "run_id", "project_id", "node_id", "script", "python", "output_dir", "params_file", "launcher",
})

# --------------------------------------------------------------------------- #
# Specs (declarative configuration)
# --------------------------------------------------------------------------- #


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NodeKind(StrEnum):
    LOCAL = "local"
    SSH = "ssh"
    SLURM = "slurm"


class SshOptions(SpecModel):
    """Non-secret SSH behaviour.

    Anything security sensitive (identity files, passwords, tokens) is
    intentionally *not* representable here: RCF relies on ``~/.ssh/config`` and
    the user's ssh-agent.  ``extra_options`` is validated against a denylist.
    """

    program: list[str] = Field(default_factory=lambda: ["ssh"])
    #: How to invoke scp.  Empty means "derive from ``program``" (ssh -> scp);
    #: tests point it at a fake binary.
    scp_program: list[str] = Field(default_factory=list)
    connect_timeout: int = Field(default=10, ge=1, le=300)
    server_alive_interval: int = Field(default=30, ge=0, le=3600)
    server_alive_count_max: int = Field(default=3, ge=1, le=20)
    batch_mode: bool = True
    extra_options: list[str] = Field(default_factory=list)

    @field_validator("program")
    @classmethod
    def _program_not_empty(cls, value: list[str]) -> list[str]:
        if not value or not value[0].strip():
            raise ValueError("ssh program must not be empty")
        return value


class SlurmCommands(SpecModel):
    sbatch: str = "sbatch"
    squeue: str = "squeue"
    sacct: str = "sacct"
    scancel: str = "scancel"
    sinfo: str = "sinfo"


class SlurmSpec(SpecModel):
    default_partition: str | None = None
    partitions: list[str] = Field(default_factory=list)
    account: str | None = None
    qos: str | None = None
    gpu_gres_template: str = "gpu:{n}"
    log_pattern: str = "slurm-%j.out"
    commands: SlurmCommands = Field(default_factory=SlurmCommands)
    # How long RCF waits for sbatch to return the job id.
    submit_timeout: int = Field(default=120, ge=10, le=1800)


class ProbeSpec(SpecModel):
    timeout: int = Field(default=45, ge=5, le=900)


class WindowsTaskSpec(SpecModel):
    """Run long jobs through Windows Task Scheduler inside WSL.

    Windows OpenSSH kills the process tree when the SSH session closes, so a
    detached ``setsid`` job does not survive. A scheduled task is owned by the
    Windows service, not by the SSH channel, and keeps one control plane.
    """

    #: WSL distribution name; empty uses the Windows default distribution.
    distro: str | None = None
    #: Windows path corresponding to ``work_root``. Omit to derive it from
    #: ``scp_path_map`` (for example ``/mnt/e`` -> ``E:/``).
    windows_work_root: str | None = None
    wsl_exe: str = r"C:\Windows\System32\wsl.exe"
    powershell: str = "powershell.exe"
    task_prefix: str = "RCF-"

    @field_validator("distro")
    @classmethod
    def _distro_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        # Task Scheduler's cmd context passes quoted -d values literally on
        # current WSL builds, so only a shell-token-safe distro name is allowed.
        if not text or not all(c.isalnum() or c in "._-" for c in text):
            raise ValueError(
                "windows_task.distro must contain only letters, digits, dot, underscore or hyphen"
            )
        return text

    @field_validator("windows_work_root")
    @classmethod
    def _windows_root_absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.replace("/", "\\").rstrip("\\")
        if len(text) < 3 or text[1] != ":" or text[2] != "\\":
            raise ValueError("windows_work_root must be an absolute Windows path like 'E:/rcf'")
        return text

    @field_validator("wsl_exe", "powershell", "task_prefix")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "\r\n\x00"):
            raise ValueError("windows_task fields must be non-empty and single-line")
        return value.strip()


class NodeSpec(SpecModel):
    id: str
    kind: NodeKind
    name: str | None = None
    description: str | None = None
    ssh_alias: str | None = None
    #: Remote root that holds per-run directories (``<work_root>/runs/<run_id>``).
    work_root: str = "~/rcf"
    labels: dict[str, str] = Field(default_factory=dict)
    os_family: str | None = None
    arch: str | None = None
    gpus_expected: bool | None = None
    slurm: SlurmSpec | None = None
    ssh: SshOptions = Field(default_factory=SshOptions)
    probe: ProbeSpec = Field(default_factory=ProbeSpec)
    #: Free-form operator notes; never interpreted by RCF.
    notes: str | None = None
    #: How this node's POSIX paths map to the path space used by ``scp``.  A
    #: Windows host running WSL executes commands inside WSL (``/mnt/e/...``)
    #: while its OpenSSH ``scp`` still speaks Windows paths (``E:/...``).
    scp_path_map: dict[str, str] = Field(default_factory=dict)
    #: Present when this SSH node runs inside WSL on a Windows host. Long jobs
    #: are launched by Windows Task Scheduler instead of the SSH session.
    windows_task: WindowsTaskSpec | None = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, value: str) -> str:
        if not NODE_ID_RE.match(value):
            raise ValueError(
                "node id must match ^[a-z0-9][a-z0-9_.-]{0,47}$ (lowercase, no spaces)"
            )
        return value

    @field_validator("work_root")
    @classmethod
    def _work_root_absolute(cls, value: str, info: ValidationInfo) -> str:
        # ``work_root`` only matters for remote nodes; a local node keeps its
        # runs inside the RCF home and may leave the default in place.
        if info.data.get("kind") is NodeKind.LOCAL:
            return value.strip()
        if not (value.startswith("/") or value.startswith("~")):
            raise ValueError("work_root must be an absolute POSIX path or start with '~'")
        if ".." in value.split("/"):
            raise ValueError("work_root must not contain '..'")
        return value.rstrip("/") or "/"

    @model_validator(mode="after")
    def _consistency(self) -> NodeSpec:
        if self.kind is NodeKind.LOCAL:
            if self.ssh_alias:
                raise ValueError("local node must not define ssh_alias")
            if self.slurm is not None:
                raise ValueError("local node must not define a slurm block")
        else:
            if not self.ssh_alias:
                raise ValueError(f"{self.kind} node requires ssh_alias (an ~/.ssh/config host)")
        if self.slurm is not None and self.kind is not NodeKind.SLURM:
            raise ValueError("slurm block requires kind = 'slurm'")
        if self.kind is NodeKind.SLURM and self.slurm is None:
            raise ValueError("kind = 'slurm' requires a [nodes.slurm] block")
        if self.windows_task is not None:
            if self.kind is not NodeKind.SSH:
                raise ValueError("windows_task block requires kind = 'ssh'")
            if not self.work_root.startswith("/"):
                raise ValueError("windows_task requires an absolute WSL work_root (for example /mnt/e/rcf)")
            if not self.windows_task.windows_work_root and not self.scp_path_map:
                raise ValueError(
                    "windows_task needs windows_work_root or scp_path_map to build the Windows launcher path"
                )
        return self

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def executor_kind(self) -> str:
        """The concrete executor name, distinct from the transport kind."""
        return "windows_task" if self.windows_task is not None else self.kind.value

    def label(self, key: str, default: str | None = None) -> str | None:
        return self.labels.get(key, default)

    def runs_root(self, run_id: str) -> str:
        return posixpath.join(self.work_root, "runs", run_id)


class EnvironmentKind(StrEnum):
    VENV = "venv"
    CONDA = "conda"
    SYSTEM = "system"
    MODULE = "module"
    APPTAINER = "apptainer"


class PackageRequirement(SpecModel):
    name: str
    version: str | None = None
    optional: bool = False


class EnvironmentSpec(SpecModel):
    id: str
    kind: EnvironmentKind = EnvironmentKind.VENV
    description: str | None = None
    python: str = "python3"
    #: Interpreter used to *create* the venv when it is not on PATH (e.g. a
    #: miniforge install in your home).  ``python`` above names the binary
    #: inside the created environment.
    base_python: str | None = None
    #: Required to *create* a conda environment: the exact Python version conda
    #: should solve for (e.g. ``"3.12"``).  ``python_version`` above stays the
    #: compatibility constraint.
    conda_python: str | None = None
    python_version: str | None = None
    path: dict[str, str] = Field(default_factory=dict)
    packages: list[PackageRequirement] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    conda_root: str | None = None
    apptainer_image: str | None = None
    cuda: str | None = None
    #: Command used to prove the environment is usable (``verified`` state).
    verify_command: str | None = None
    #: Command used by the read-only inspector to print versions.
    probe_command: str | None = None
    #: Declares that this prefix belongs to RCF (needed to modify an existing
    #: environment that RCF did not create itself).
    managed: bool = False
    #: Extra arguments for the installer (e.g. ``["--only-binary=:all:"]`` on a
    #: cluster without a usable compiler, or ``--index-url`` for a mirror).
    install_args: list[str] = Field(default_factory=list)
    env_vars: dict[str, str] = Field(default_factory=dict)
    #: How to start MPI ranks for this environment, with ``{tasks}`` substituted
    #: (e.g. ``"srun --mpi=pmi2 -n {tasks}"``).  Leave unset to let RCF choose:
    #: ``mpiexec`` when the environment ships one, otherwise ``srun`` on Slurm and
    #: ``mpirun`` elsewhere.
    mpi_launcher: str | None = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, value: str) -> str:
        if not IDENT_RE.match(value):
            raise ValueError("environment id must match ^[a-z0-9][a-z0-9_.-]{0,47}$")
        return value

    def path_for(self, node_id: str) -> str | None:
        return self.path.get(node_id) or self.path.get("*")


class OutputContract(StrEnum):
    """How a project promises to write its artifacts.

    ``argument`` — the entrypoint passes ``{output_dir}`` to the script (the
    default; RCF refuses to plan otherwise).
    ``env``      — the script reads ``$RCF_OUTPUT_DIR`` itself.
    ``none``     — the script cannot be changed (or writes elsewhere on
    purpose); RCF only reports where the files went.
    """

    ARGUMENT = "argument"
    ENV = "env"
    NONE = "none"


class ProjectSpec(SpecModel):
    id: str
    name: str | None = None
    description: str | None = None
    git_url: str | None = None
    default_branch: str = "main"
    commit: str | None = None
    #: ``{node_id: posix path}``; ``"*"`` is used as a fallback.
    paths: dict[str, str] = Field(default_factory=dict)
    data_paths: dict[str, str] = Field(default_factory=dict)
    env_id: str | None = None
    #: Per-node environment override.  One project often spans a workstation
    #: (conda/venv) and an HPC login node (modules); a single env_id cannot
    #: describe both.
    env_by_node: dict[str, str] = Field(default_factory=dict)
    entrypoint: str = "python {script}"
    script: str | None = None
    outputs: str = "outputs"
    #: Extra patterns to skip when pushing this project to a node (on top of the
    #: built-in cache/venv/output list).
    sync_exclude: list[str] = Field(default_factory=list)
    #: Optional source allowlist; never implies deletion of excluded target files.
    sync_include: list[str] = Field(default_factory=list)
    #: Explicitly disposable files, relative to a run's declared result directory.
    disposable_globs: list[str] = Field(default_factory=list)
    #: Soft storage warnings; zero means unset. No budget authorizes deletion.
    storage_budget_bytes: int = Field(default=0, ge=0)
    storage_budget_files: int = Field(default=0, ge=0)
    storage_keep_recent_runs: int | None = Field(default=None, ge=0)
    #: Input files this project reads, as POSIX globs relative to the checkout
    #: (``"data/*.dat"``).  Declared inputs are resolved at plan time: a pattern
    #: that matches nothing blocks the run instead of failing halfway through.
    inputs: list[str] = Field(default_factory=list)
    #: How artifacts reach the run directory.  Defaults to the strict contract.
    output_contract: OutputContract = OutputContract.ARGUMENT
    progress: ProgressContract | None = None
    runtime_budget_seconds: float | None = Field(default=None, gt=0, le=2592000, allow_inf_nan=False)
    verify_command: str | None = None
    required_labels: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, value: str) -> str:
        if not IDENT_RE.match(value):
            raise ValueError("project id must match ^[a-z0-9][a-z0-9_.-]{0,47}$")
        return value

    def path_for(self, node_id: str) -> str | None:
        return self.paths.get(node_id) or self.paths.get("*")

    def data_path_for(self, node_id: str) -> str | None:
        """Where this project's data lives on a node (never inside the checkout)."""

        return self.data_paths.get(node_id) or self.data_paths.get("*")


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #


class SyncFile(RuntimeModel):
    path: str
    size: int
    mtime: float
    sha256: str | None = None


class PushPlan(RuntimeModel):
    """What `project push` would change on the target node."""

    project_id: str
    #: ``code`` copies the checkout; ``data`` copies the data root.
    scope: str = "code"
    source_node: str
    target_node: str
    source_root: str
    target_root: str
    new: list[SyncFile] = Field(default_factory=list)
    changed: list[SyncFile] = Field(default_factory=list)
    #: Files that exist on the target, are newer there, and are therefore left
    #: alone unless `force` is set.
    remote_newer: list[SyncFile] = Field(default_factory=list)
    unchanged: int = 0
    bytes: int = 0
    excludes: list[str] = Field(default_factory=list)
    force: bool = False
    checks: list[Check] = Field(default_factory=list)

    @property
    def pending(self) -> int:
        return len(self.new) + len(self.changed)


class PushReport(RuntimeModel):
    project_id: str
    source_node: str
    target_node: str
    target_root: str
    files: int
    bytes: int
    unchanged: int = 0
    archive_sha256: str | None = None
    verified: bool = False
    #: Exact paths and hashes that were verified after this deployment.
    entries: list[SyncFile] = Field(default_factory=list)


class ProbeStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class Check(RuntimeModel):
    name: str
    status: ProbeStatus
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status is ProbeStatus.FAIL


class CpuInfo(RuntimeModel):
    logical_cores: int | None = None
    physical_cores: int | None = None
    model: str | None = None
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None


class MemoryInfo(RuntimeModel):
    total_mb: int | None = None
    available_mb: int | None = None
    used_mb: int | None = None


class DiskInfo(RuntimeModel):
    path: str
    total_kb: int | None = None
    used_kb: int | None = None
    avail_kb: int | None = None


class GpuProcess(RuntimeModel):
    pid: int | None = None
    name: str | None = None
    used_memory_mb: int | None = None
    gpu_uuid: str | None = None


class GpuInfo(RuntimeModel):
    index: int
    uuid: str | None = None
    name: str | None = None
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    utilization_pct: int | None = None
    driver_version: str | None = None
    processes: list[GpuProcess] = Field(default_factory=list)


class SlurmPartition(RuntimeModel):
    name: str
    state: str | None = None
    avl_nodes: int | None = None
    total_nodes: int | None = None
    cpus: int | None = None
    memory_mb: int | None = None
    gres: str | None = None
    time_limit: str | None = None


class SlurmJob(RuntimeModel):
    job_id: str
    name: str | None = None
    state: str | None = None
    partition: str | None = None
    user: str | None = None
    nodes: int | None = None
    cpus: int | None = None
    gpus: int | None = None
    elapsed: str | None = None
    node_list: str | None = None
    reason: str | None = None
    exit_code: str | None = None


class SchedulerView(RuntimeModel):
    available: bool = False
    partitions: list[SlurmPartition] = Field(default_factory=list)
    user_jobs: list[SlurmJob] = Field(default_factory=list)
    reason: str | None = None


class ResourceSnapshot(RuntimeModel):
    """One observation of a node.

    The four resource dimensions are kept separate on purpose: physical
    capacity, current usage, scheduler allocation and account entitlement are
    different things and must not be conflated by the planner.
    """

    node_id: str
    observed_at: datetime
    adapter: str
    #: ``$HOME`` as reported by the node (lets RCF resolve ``~`` in work_root).
    home_dir: str | None = None
    cpu: CpuInfo | None = None
    memory: MemoryInfo | None = None
    disks: list[DiskInfo] = Field(default_factory=list)
    gpus: list[GpuInfo] = Field(default_factory=list)
    gpu_tool: ProbeStatus = ProbeStatus.UNKNOWN
    scheduler: SchedulerView = Field(default_factory=SchedulerView)
    checks: list[Check] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_s: float | None = None

    @property
    def blocking_issues(self) -> list[Check]:
        return [c for c in self.checks if c.status is ProbeStatus.FAIL]

    def gpu_free_memory_mb(self) -> list[int | None]:
        return [
            None
            if g.memory_total_mb is None or g.memory_used_mb is None
            else max(0, g.memory_total_mb - g.memory_used_mb)
            for g in self.gpus
        ]


class GpuSchedulabilityStatus(StrEnum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    SCHEDULER_GRANTED = "scheduler_granted"
    UNKNOWN = "unknown"
    ABSENT = "absent"


class GpuSchedulability(RuntimeModel):
    """Whether a GPU could be handed to a new job *now*.

    Free VRAM is never treated as availability: a device with idle memory can
    still be reserved by Slurm or held by another process.
    """

    gpu_index: int
    status: GpuSchedulabilityStatus
    reasons: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Environments / projects (runtime views)
# --------------------------------------------------------------------------- #


class EnvironmentState(StrEnum):
    INSTALLED = "installed"
    COMPATIBLE = "compatible"
    VERIFIED = "verified"
    MISSING = "missing"
    UNKNOWN = "unknown"


class EnvironmentView(RuntimeModel):
    env_id: str
    node_id: str
    kind: EnvironmentKind
    state: EnvironmentState
    path: str | None = None
    python_version: str | None = None
    fingerprint: str | None = None
    #: Hash over *every* installed distribution, so a transitive upgrade that
    #: changes numerics is visible even when the declared packages are unchanged.
    env_digest: str | None = None
    dist_count: int | None = None
    packages: dict[str, str | None] = Field(default_factory=dict)
    missing_packages: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    verified_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class EnvironmentStep(RuntimeModel):
    description: str
    command: list[str]
    mutating: bool = True
    requires_network: bool = False


class EnvironmentPlan(RuntimeModel):
    env_id: str
    node_id: str
    action: Literal["none", "create", "install", "blocked"]
    steps: list[EnvironmentStep] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    requires_authorization: bool = True
    notes: list[str] = Field(default_factory=list)


class ProjectView(RuntimeModel):
    project_id: str
    node_id: str
    path: str | None
    exists: ProbeStatus
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    checks: list[Check] = Field(default_factory=list)


class SyncPlan(RuntimeModel):
    project_id: str
    node_id: str
    path: str | None
    steps: list[EnvironmentStep] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    current_commit: str | None = None
    target_commit: str | None = None
    requires_authorization: bool = True


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class ResourceRequirements(SpecModel):
    gpus: int = Field(default=0, ge=0, le=64)
    #: MPI ranks. 1 means one process. On Slurm this becomes ``--ntasks`` and the
    #: ``{launcher}`` placeholder expands to ``srun``; elsewhere to ``mpirun -np``.
    tasks: int = Field(default=1, ge=1, le=1024)
    cpus: int | None = Field(default=None, ge=1, le=1024)
    memory_gb: int | None = Field(default=None, ge=1, le=4096)
    time_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    partition: str | None = None
    gpu_memory_gb: int | None = Field(default=None, ge=1, le=1024)
    #: Allow sharing a GPU with another RCF run (explicit, never implicit).
    allow_shared_gpu: bool = False


class RunRequest(SpecModel):
    project_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)
    node_id: str | None = None
    env_id: str | None = None
    command_override: str | None = None
    notes: str | None = None
    pilot_seconds: int | None = Field(default=None, ge=1, le=300)
    runtime_budget_seconds: float | None = Field(default=None, gt=0, le=2592000, allow_inf_nan=False)

    @field_validator("params")
    @classmethod
    def _params_scalar(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if key in RESERVED_RUN_PARAMETERS:
                raise ValueError(f"parameter {key!r} is reserved for RCF-generated execution values")
            if not isinstance(key, str):
                raise ValueError("parameter keys must be strings")
            if isinstance(item, (dict, list)):
                # Kept simple and predictable: parameters are scalars only.
                raise ValueError(f"parameter {key!r} must be a scalar (str/int/float/bool/null)")
        return value


class PlannedCommand(RuntimeModel):
    """Everything needed to reproduce a command line, in reviewable form."""

    argv: list[str]
    workdir: str
    env: dict[str, str] = Field(default_factory=dict)
    #: POSIX shell lines that must run before ``argv`` (module load, conda ...).
    preamble: list[str] = Field(default_factory=list)
    shell: bool = False
    preview: str = ""


class InputRoot(StrEnum):
    PROJECT = "project"
    DATA = "data"


class InputFile(RuntimeModel):
    #: ``project`` for files inside the checkout, ``data`` for files under
    #: ``data_paths``.  Kept apart so code and data can be moved independently.
    root: InputRoot = InputRoot.PROJECT
    path: str
    bytes: int


class InputManifest(RuntimeModel):
    """What a run reads, resolved when the plan was made.

    Code-relative and data-relative inputs stay distinct: the two trees move by
    different routes (git/push for code, a separate data root for data), so a
    manifest that blurred them could not be acted on.
    """

    digest: str
    total_bytes: int
    files: list[InputFile] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        project: dict[str, list[tuple[str, int]]] | None = None,
        data: dict[str, list[tuple[str, int]]] | None = None,
    ) -> InputManifest:
        files: list[InputFile] = []
        for root, entries in ((InputRoot.PROJECT, project or {}), (InputRoot.DATA, data or {})):
            for pattern in sorted(entries):
                for path, size in sorted(entries[pattern]):
                    files.append(InputFile(root=root, path=path, bytes=size))
        listing = "\n".join(f"{f.root.value}:{f.path}\t{f.bytes}" for f in files)
        digest = hashlib.sha256(listing.encode("utf-8")).hexdigest()
        return cls(digest=digest, total_bytes=sum(f.bytes for f in files), files=files)

    @property
    def data_files(self) -> list[InputFile]:
        return [f for f in self.files if f.root is InputRoot.DATA]


class RunPlan(RuntimeModel):
    execution_timeout_seconds: int | None = None
    progress_contract: ProgressContract | None = None
    calibration_key: str | None = None
    progress_total: float | None = None
    plan_id: str
    #: Allocated at plan time so the output directory is reviewable before submit.
    run_id: str
    created_at: datetime
    request: RunRequest
    node_id: str
    node_kind: NodeKind
    executor: str
    #: Slurm partition actually used (chosen when the request did not name one).
    partition: str | None = None
    command: PlannedCommand
    workdir: str
    project_path: str | None = None
    #: Commit checked out on the node at plan time (None when not a git repo).
    git_commit: str | None = None
    git_dirty: bool | None = None
    run_dir: str | None = None
    result_dir: str | None = None
    #: Declared inputs resolved at plan time (None when the project declares none).
    inputs: InputManifest | None = None
    env_id: str | None = None
    env_fingerprint: str | None = None
    resources: ResourceSnapshot | None = None
    checks: list[Check] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    gpu_leases: list[str] = Field(default_factory=list)
    requires_authorization: bool = True
    fingerprint: str = ""

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.status is ProbeStatus.FAIL]

    @property
    def executable(self) -> bool:
        return not self.blockers


class RunState(StrEnum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.LOST,
        }


TERMINAL_STATES = (
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.LOST,
)


class JobHandle(RuntimeModel):
    """Opaque, persistent pointer to a submitted job.

    It must be sufficient to poll a job after the controlling process (or the
    SSH connection) has gone away.
    """

    kind: str
    node_id: str
    job_id: str | None = None
    workdir: str | None = None
    log_path: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class JobSpec(RuntimeModel):
    run_id: str
    #: Directory that holds the job's own state files (not the project checkout).
    run_dir: str
    command: PlannedCommand
    log_path: str
    resources: ResourceRequirements
    project_id: str | None = None
    env_id: str | None = None
    time_minutes: int | None = None
    result_dir: str | None = None
    #: Free-form, persisted next to the job so a run stays self-describing.
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobStatus(RuntimeModel):
    state: RunState
    exit_code: int | None = None
    detail: str = ""
    #: When RCF looked at the job.
    observed_at: datetime
    #: When the job itself ended, if the executor can tell (probe file or sacct).
    #: ``observed_at`` is *not* a substitute: polling lag can be hours.
    finished_at: datetime | None = None
    scheduler_state: str | None = None
    node_list: str | None = None


class RunRecord(RuntimeModel):
    run_id: str
    plan_id: str | None = None
    project_id: str | None = None
    node_id: str
    executor: str
    state: RunState
    created_at: datetime
    submitted_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    command: PlannedCommand | None = None
    workdir: str | None = None
    log_path: str | None = None
    result_dir: str | None = None
    inputs: InputManifest | None = None
    env_id: str | None = None
    env_fingerprint: str | None = None
    #: Code version this run executed (None when the checkout is not a repo).
    git_commit: str | None = None
    git_dirty: bool | None = None
    handle: JobHandle | None = None
    idempotency_key: str | None = None
    error: str | None = None
    #: True when the run finished but left nothing in its output directory,
    #: i.e. the project's artifact contract was not honoured.
    contract_violation: bool = False
    last_observed_at: datetime | None = None
    #: When the saved *state* was last confirmed by a successful probe.
    #: ``last_observed_at`` records attempts (including failed ones), so it can
    #: be fresh while the state itself is days old.
    state_observed_at: datetime | None = None


class RunEvent(RuntimeModel):
    run_id: str
    at: datetime
    kind: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ResultManifest(RuntimeModel):
    run_id: str
    node_id: str
    result_dir: str | None
    entries: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    #: Files that changed in the project checkout during the run.  Only filled
    #: when the declared output directory is empty, i.e. the script wrote its
    #: results next to the sources.  They are *reported*, not collected.
    workdir_changes: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""


class SubmissionReceipt(RuntimeModel):
    handle: JobHandle
    state: RunState
    raw: str = ""
