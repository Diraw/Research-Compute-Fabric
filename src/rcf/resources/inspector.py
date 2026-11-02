"""Resource inspection and GPU schedulability assessment.

The important distinction (from the brief): free VRAM is **not** availability.
A GPU with 70 GB free can still be reserved by Slurm, or held by a process that
maps memory lazily.  ``assess_gpus`` therefore reports *evidence + status*, and
the planner refuses to hand a device to a job unless the evidence says it is
actually free — or a scheduler will arbitrate it.
"""

from __future__ import annotations

import time

from ..core.config import Config
from ..core.models import (
    Check,
    GpuInfo,
    GpuSchedulability,
    GpuSchedulabilityStatus,
    NodeKind,
    NodeSpec,
    ProbeStatus,
    ResourceSnapshot,
)
from ..core.nodeops import NodeOps
from ..core.runner import Runner
from .probes import LocalProbe, PosixRemoteProbe, ProbeOutcome

#: Shared-memory use above this fraction counts as "the device is in use".
GPU_MEMORY_BUSY_FRACTION = 0.05


def gpu_key(gpu: GpuInfo) -> str:
    return gpu.uuid or f"index:{gpu.index}"


class ResourceInspector:
    def __init__(
        self,
        config: Config,
        runner: Runner,
        *,
        cache_seconds: int | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.cache_seconds = (
            cache_seconds if cache_seconds is not None else config.settings.defaults.inspect_cache_seconds
        )
        self._cache: dict[str, tuple[float, ProbeOutcome]] = {}
        self.ops = NodeOps(runner)

    # -- probing ------------------------------------------------------------ #
    def probe(self, node: NodeSpec) -> ProbeOutcome:
        if node.kind is NodeKind.LOCAL:
            return LocalProbe(self.runner, node).probe()
        from ..executors.ssh import SshTransport

        transport = SshTransport.from_node(node, self.runner)
        return PosixRemoteProbe(transport, node).probe()

    def inspect(self, node: NodeSpec, *, refresh: bool = False) -> ResourceSnapshot:
        cached = self._cache.get(node.id)
        now = time.monotonic()
        if not refresh and cached and (now - cached[0]) < self.cache_seconds:
            return cached[1].snapshot
        outcome = self.probe(node)
        self._cache[node.id] = (now, outcome)
        return outcome.snapshot

    def invalidate(self, node_id: str | None = None) -> None:
        if node_id is None:
            self._cache.clear()
        else:
            self._cache.pop(node_id, None)

    # -- GPU assessment ----------------------------------------------------- #
    def assess_gpus(
        self,
        node: NodeSpec,
        snapshot: ResourceSnapshot,
        *,
        leases: dict[str, str] | None = None,
        own_run_id: str | None = None,
    ) -> list[GpuSchedulability]:
        leases = leases or {}
        if not snapshot.gpus:
            return []

        results: list[GpuSchedulability] = []
        for gpu in snapshot.gpus:
            key = gpu_key(gpu)
            if node.kind is NodeKind.SLURM:
                results.append(
                    GpuSchedulability(
                        gpu_index=gpu.index,
                        status=GpuSchedulabilityStatus.SCHEDULER_GRANTED,
                        reasons=[
                            "GPU access on this node is granted by Slurm (sbatch --gres); "
                            "login-node nvidia-smi is not used to decide availability"
                        ],
                        evidence={"uuid": gpu.uuid, "gres_request": node.slurm.gpu_gres_template if node.slurm else None},
                    )
                )
                continue

            reasons: list[str] = []
            holder = leases.get(key)
            if holder and holder != own_run_id:
                results.append(
                    GpuSchedulability(
                        gpu_index=gpu.index,
                        status=GpuSchedulabilityStatus.OCCUPIED,
                        reasons=[f"reserved by RCF run {holder}"],
                        evidence={"lease": holder},
                    )
                )
                continue
            if gpu.processes:
                pids = [str(p.pid) for p in gpu.processes if p.pid][:5]
                free_mb = max(0, (gpu.memory_total_mb or 0) - (gpu.memory_used_mb or 0))
                results.append(
                    GpuSchedulability(
                        gpu_index=gpu.index,
                        status=GpuSchedulabilityStatus.OCCUPIED,
                        reasons=[
                            f"{len(gpu.processes)} compute process(es) active "
                            f"(pid {', '.join(pids)}{'...' if len(gpu.processes) > len(pids) else ''}); "
                            f"{free_mb} MB VRAM free"
                        ],
                        evidence={
                            "processes": [p.model_dump() for p in gpu.processes],
                            "free_mb": free_mb,
                            "memory_total_mb": gpu.memory_total_mb,
                        },
                    )
                )
                continue
            total = gpu.memory_total_mb or 0
            used = gpu.memory_used_mb or 0
            if total and used / total > GPU_MEMORY_BUSY_FRACTION:
                results.append(
                    GpuSchedulability(
                        gpu_index=gpu.index,
                        status=GpuSchedulabilityStatus.UNKNOWN,
                        reasons=[
                            f"{used} MB of {total} MB already allocated without a visible compute process "
                            "(container, other user or driver reservation)"
                        ],
                        evidence={
                            "memory_used_mb": used,
                            "memory_total_mb": total,
                            "free_mb": total - used,
                        },
                    )
                )
                continue
            reasons.append("no compute process and no RCF lease observed")
            results.append(
                GpuSchedulability(
                    gpu_index=gpu.index,
                    status=GpuSchedulabilityStatus.AVAILABLE,
                    reasons=reasons + ["advisory: RCF cannot reserve a device it does not control"],
                    evidence={"memory_used_mb": used, "memory_total_mb": total, "uuid": gpu.uuid},
                )
            )
        return results

    def select_gpus(
        self,
        node: NodeSpec,
        snapshot: ResourceSnapshot,
        assessments: list[GpuSchedulability],
        count: int,
        *,
        required_vram_mb: int | None = None,
        allow_shared: bool = False,
    ) -> tuple[list[GpuSchedulability], list[str]]:
        """Pick ``count`` GPUs; returns (selection, blocking reasons).

        A device that is busy is only accepted when the caller stated how much
        VRAM it needs *and* it actually fits — and, for a device another process
        is computing on, only with ``allow_shared=True``.  Nothing here is a
        reservation: the selectable path is "evidence says it fits".
        """

        if count <= 0:
            return [], []
        if node.kind is NodeKind.SLURM:
            return assessments[:count], []

        def free_mb(assessment: GpuSchedulability) -> int:
            gpu = snapshot.gpus[assessment.gpu_index] if assessment.gpu_index < len(snapshot.gpus) else None
            return _free_mb(gpu)

        usable: list[GpuSchedulability] = []
        for assessment in assessments:
            fits = required_vram_mb is not None and free_mb(assessment) >= required_vram_mb
            if assessment.status is GpuSchedulabilityStatus.AVAILABLE:
                if required_vram_mb is None or fits:
                    usable.append(assessment)
            elif assessment.status is GpuSchedulabilityStatus.UNKNOWN and fits:
                usable.append(assessment)
            elif assessment.status is GpuSchedulabilityStatus.OCCUPIED and fits and allow_shared:
                usable.append(assessment)
        usable.sort(key=free_mb, reverse=True)
        if len(usable) < count:
            reasons = [f"only {len(usable)} of {count} requested GPU(s) usable on {node.id!r}"]
            for assessment in assessments:
                if assessment in usable:
                    continue
                hint = ""
                if assessment.status is GpuSchedulabilityStatus.OCCUPIED and not allow_shared:
                    hint = " — pass gpu_memory_gb and allow_shared_gpu to share it explicitly"
                reasons.append(
                    f"gpu{assessment.gpu_index}: {assessment.status.value} "
                    f"({'; '.join(assessment.reasons)}){hint}"
                )
            return usable, reasons
        return usable[:count], []

    # -- composite checks --------------------------------------------------- #
    def resource_checks(
        self,
        node: NodeSpec,
        snapshot: ResourceSnapshot,
        *,
        cpus: int | None,
        memory_gb: int | None,
        gpus: int,
        gpu_memory_gb: int | None,
        disk_kb: int | None = None,
    ) -> list[Check]:
        checks: list[Check] = []
        if cpus and snapshot.cpu and snapshot.cpu.logical_cores:
            ok = snapshot.cpu.logical_cores >= cpus
            checks.append(
                Check(
                    name="cpu_capacity",
                    status=ProbeStatus.OK if ok else ProbeStatus.FAIL,
                    detail=f"{snapshot.cpu.logical_cores} logical cores vs {cpus} requested",
                )
            )
            if node.kind is not NodeKind.SLURM and snapshot.cpu.load_1m is not None:
                headroom = snapshot.cpu.logical_cores - snapshot.cpu.load_1m
                checks.append(
                    Check(
                        name="cpu_load",
                        status=ProbeStatus.OK if headroom >= cpus else ProbeStatus.WARN,
                        detail=f"load1 {snapshot.cpu.load_1m} on {snapshot.cpu.logical_cores} cores",
                    )
                )
        if memory_gb and snapshot.memory and snapshot.memory.available_mb:
            available_gb = snapshot.memory.available_mb / 1024
            ok = available_gb >= memory_gb
            checks.append(
                Check(
                    name="memory_capacity",
                    status=ProbeStatus.OK if ok else ProbeStatus.FAIL,
                    detail=f"{available_gb:.1f} GB available vs {memory_gb} GB requested"
                    + (" (Slurm may allocate beyond current usage)" if node.kind is NodeKind.SLURM else ""),
                )
            )
        if gpus:
            if node.kind is NodeKind.SLURM:
                checks.append(
                    Check(
                        name="gpu_request",
                        status=ProbeStatus.OK,
                        detail=f"{gpus} GPU(s) requested through the scheduler",
                    )
                )
            elif gpu_memory_gb:
                candidates = [
                    g
                    for g in snapshot.gpus
                    if (g.memory_total_mb or 0) >= gpu_memory_gb * 1024
                ]
                checks.append(
                    Check(
                        name="gpu_memory",
                        status=ProbeStatus.OK if len(candidates) >= gpus else ProbeStatus.FAIL,
                        detail=f"{len(candidates)} GPU(s) with >= {gpu_memory_gb} GB VRAM",
                    )
                )
        if disk_kb is not None and disk_kb > 0:
            free = min((d.avail_kb or 0) for d in snapshot.disks) if snapshot.disks else 0
            checks.append(
                Check(
                    name="disk_capacity",
                    status=ProbeStatus.OK if free >= disk_kb else ProbeStatus.WARN,
                    detail=f"{free / 1024 / 1024:.1f} GiB free, {disk_kb / 1024 / 1024:.1f} GiB requested",
                )
            )
        return checks


def _free_mb(gpu: GpuInfo | None) -> int:
    if gpu is None or gpu.memory_total_mb is None:
        return 0
    return max(0, gpu.memory_total_mb - (gpu.memory_used_mb or 0))
