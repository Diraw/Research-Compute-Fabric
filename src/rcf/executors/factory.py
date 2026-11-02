"""Build the executor that matches a node declaration."""

from __future__ import annotations

from pathlib import Path

from ..core.config import Config
from ..core.errors import UnsupportedOperation
from ..core.models import NodeKind, NodeSpec
from ..core.runner import Runner
from .base import Executor
from .local_exec import LocalExecutor
from .slurm_exec import SlurmExecutor
from .ssh import SshTransport
from .ssh_exec import SshExecutor
from .windows_task import WindowsTaskExecutor


def build_executor(config: Config, node: NodeSpec, runner: Runner) -> Executor:
    if node.kind is NodeKind.LOCAL:
        return LocalExecutor(node, Path(config.home) / "runs", runner)
    transport = SshTransport.from_node(node, runner)
    if node.kind is NodeKind.SSH:
        if node.windows_task is not None:
            return WindowsTaskExecutor(node, transport)
        return SshExecutor(node, transport)
    if node.kind is NodeKind.SLURM:
        return SlurmExecutor(node, transport)
    raise UnsupportedOperation(f"no executor for node kind {node.kind!r}")  # pragma: no cover


def local_run_dir(config: Config, run_id: str) -> Path:
    return Path(config.home) / "runs" / run_id
