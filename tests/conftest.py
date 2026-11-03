"""Shared fixtures: config homes, node specs and the simulated SSH/Slurm host."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from bash_support import find_test_bash

from rcf.core.config import Config
from rcf.core.models import NodeSpec, ResourceRequirements, RunRequest
from rcf.core.runner import SubprocessRunner
from rcf.service import RcfService

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON = sys.executable
CAT_BIN = "/bin/cat" if sys.platform == "darwin" else "/usr/bin/cat"
HEAD_BIN = "/usr/bin/head"

DEFAULT_SETTINGS = """\
[security]
allow_execute = true
allow_environment_mutation = false
allow_system_admin = false
require_confirmation = true

[defaults]
inspect_cache_seconds = 0
probe_timeout = 30
"""

HELLO_PY = """import argparse
import os
import pathlib

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()

out = pathlib.Path(os.environ["RCF_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "result.txt").write_text(
    "seed=%d run=%s" % (args.seed, os.environ["RCF_RUN_ID"]) + chr(10), encoding="utf-8"
)
print("hello", args.seed, os.environ["RCF_RUN_ID"])
"""

MEMINFO = (
    "MemTotal:       64000000 kB\\n"
    "MemFree:        32000000 kB\\n"
    "MemAvailable:   48000000 kB\\n"
)


def toml_path(path: str | Path) -> str:
    """TOML-safe path: forward slashes (backslashes would be escape sequences)."""

    return str(path).replace("\\", "/")


def to_win(path: str | Path) -> Path:
    """Map a git-bash path (``/c/Users/...``) back to a Windows path for assertions."""

    text = str(path)
    if len(text) > 2 and text[0] == "/" and text[2] == "/" and text[1].isalpha():
        text = text[1].upper() + ":" + text[2:]
    return Path(text)


def posix_fs(path: str | Path) -> str:
    """Path in the form a POSIX shell inside git-bash expects (``/e/tmp/...``)."""

    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o755)
    except OSError:  # pragma: no cover - windows
        pass
    return path


def make_home(
    tmp_path: Path,
    *,
    nodes: str = "",
    projects: str = "",
    environments: str = "",
    settings: str = DEFAULT_SETTINGS,
    legacy_progress: bool = True,
) -> Path:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    # Legacy fixtures intentionally test pre-contract behavior. Production
    # defaults stay strict; new progress tests opt in with legacy_progress=False.
    if legacy_progress and "[progress]" not in settings:
        settings += "\n[progress]\nenforce_long_runs = false\n"
    (home / "rcf.toml").write_text(settings, encoding="utf-8")
    if nodes:
        (home / "nodes.toml").write_text(nodes, encoding="utf-8")
    if projects:
        (home / "projects.toml").write_text(projects, encoding="utf-8")
    if environments:
        (home / "environments.toml").write_text(environments, encoding="utf-8")
    return home


def local_node(node_id: str = "laptop", **labels: str) -> NodeSpec:
    return NodeSpec(id=node_id, kind="local", labels=labels or {"site": "laptop"})


def ssh_node(node_id: str = "workstation", alias: str | None = None) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        kind="ssh",
        ssh_alias=alias or node_id,
        work_root="/home/researcher/rcf",
        labels={"site": "office", "gpu": "yes"},
        gpus_expected=True,
    )


def run_request(project_id: str = "hello", **kwargs) -> RunRequest:
    payload = {"project_id": project_id, "params": {}, **kwargs}
    payload.setdefault("requirements", ResourceRequirements())
    return RunRequest.model_validate(payload)


@dataclass
class Sandbox:
    root: Path
    host: Path

    @property
    def home_dir(self) -> str:
        return f"{self.host.as_posix()}"

    @property
    def project_path(self) -> str:
        return str(self.host / "proj" / "hello").replace("\\", "/")

    @property
    def run_dirs(self) -> Path:
        return self.host / "rcf" / "runs"


@pytest.fixture
def bash_binary() -> str:
    candidate = find_test_bash()
    if not candidate:
        pytest.skip("no suitable Bash (Git Bash on Windows); set RCF_TEST_BASH to override")
    return candidate


@pytest.fixture
def ssh_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bash_binary: str) -> Sandbox:
    """A directory tree that behaves like a Slurm login node behind ssh."""

    root = tmp_path / "remote"
    host = root / "lab-hpc"
    (host / "rcf" / "runs").mkdir(parents=True, exist_ok=True)
    project_dir = host / "proj" / "hello"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    for tool in ("sbatch", "squeue", "sacct", "scancel", "sinfo"):
        write_executable(
            bin_dir / tool,
            f'#!/usr/bin/env bash\nexec "{PYTHON}" "{FIXTURES / "fake_slurm.py"}" {tool} "$@"\n',
        )
    write_executable(
        bin_dir / "nvidia-smi",
        f'#!/usr/bin/env bash\nexec "{PYTHON}" "{FIXTURES / "fake_nvidia_smi.py"}" "$@"\n',
    )
    write_executable(
        bin_dir / "cat",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "/proc/meminfo" ]; then\n'
        f"  printf '{MEMINFO}'\n"
        "else\n"
        f'  exec {CAT_BIN} "$@"\n'
        "fi\n",
    )
    write_executable(
        bin_dir / "head",
        "#!/usr/bin/env bash\n"
        'if [ "$3" = "/proc/meminfo" ] || [ "$1" = "/proc/meminfo" ]; then\n'
        f"  printf '{MEMINFO}'\n"
        "else\n"
        f'  exec {HEAD_BIN} "$@"\n'
        "fi\n",
    )
    write_executable(
        bin_dir / "env.sh",
        f'export PATH="{posix_fs(bin_dir)}":$PATH' + chr(10),
    )
    for name in ("python", "python3"):
        write_executable(bin_dir / name, f'#!/usr/bin/env bash\nexec "{PYTHON}" "$@"\n')
    write_executable(bin_dir / "module", "#!/usr/bin/env bash\nexit 0\n")

    monkeypatch.setenv("RCF_FAKE_SSH_ROOT", str(root))
    monkeypatch.setenv("RCF_TEST_BASH", bash_binary)
    monkeypatch.setenv("FAKE_SLURM_DIR", str(host / ".fake_slurm"))
    monkeypatch.delenv("RCF_FAKE_SSH_MODE", raising=False)
    monkeypatch.delenv("FAKE_SLURM_MODE", raising=False)
    monkeypatch.delenv("FAKE_GPU", raising=False)
    return Sandbox(root=root, host=host)


@pytest.fixture
def slurm_home(tmp_path: Path, ssh_sandbox: Sandbox) -> Path:
    """A config home with one simulated Slurm node and one project."""

    nodes = f"""\
[nodes.lab-hpc]
kind = "slurm"
name = "Simulated lab cluster"
ssh_alias = "lab-hpc"
work_root = "~/rcf"
labels = {{ site = "lab", gpu = "yes" }}
gpus_expected = true

[nodes.lab-hpc.ssh]
program = ["{toml_path(PYTHON)}", "{toml_path(FIXTURES / "fake_ssh.py")}"]
scp_program = ["{toml_path(PYTHON)}", "{toml_path(FIXTURES / "fake_scp.py")}"]

[nodes.lab-hpc.slurm]
default_partition = "gpu"
partitions = ["gpu", "cpu"]
account = "research"
"""
    projects = f"""\
[projects.hello]
entrypoint = "{{python}} -m hello --seed {{seed}}"
output_contract = "env"
script = "hello.py"
outputs = "outputs"
env_id = "py311"
paths = {{ "lab-hpc" = "{toml_path(ssh_sandbox.project_path)}" }}
"""
    environments = """\
[environments.py311]
kind = "system"
python = "python3"
python_version = ">=3.10"
packages = [{ name = "pytest", version = ">=8" }]
"""
    return make_home(tmp_path, nodes=nodes, projects=projects, environments=environments)


@pytest.fixture
def service(slurm_home: Path) -> RcfService:
    svc = RcfService(Config.load(slurm_home), runner=SubprocessRunner(), actor="test")
    yield svc
    svc.close()
