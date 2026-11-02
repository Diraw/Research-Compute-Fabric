"""Resource probing.

Two backends:

* :class:`PosixRemoteProbe` — one batched shell script executed through a
  :class:`PosixShell` (an SSH transport).  A single round trip keeps inspection
  fast on slow links and makes the transcript reproducible in tests.
* :class:`LocalProbe` — the same information gathered with the Python standard
  library, so RCF works on the machine it runs on without assuming a POSIX
  shell (Windows included).

A probe never raises for a *missing* capability: it records an explicit
``Check`` so the caller can distinguish "no GPU" from "probe failed".
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..core.errors import CommandTimedOut
from ..core.ids import now_utc
from ..core.models import (
    Check,
    CpuInfo,
    DiskInfo,
    GpuInfo,
    MemoryInfo,
    NodeKind,
    NodeSpec,
    ProbeStatus,
    ResourceSnapshot,
    SchedulerView,
)
from ..core.runner import CommandResult, Runner
from . import parsers

PROBE_MARKER = "@@"


class PosixShell(Protocol):
    def run(self, command: str, *, timeout: float | None = None) -> CommandResult: ...


def build_probe_script(*, include_slurm: bool = True) -> str:
    """One self-contained POSIX script; degrades quietly when tools are absent."""

    lines = [
        'echo "@@home"',
        'echo "$HOME" 2>/dev/null || true',
        'echo "@@host"',
        "uname -srm 2>/dev/null || true",
        'echo "@@cpu"',
        "nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || true",
        'echo "@@loadavg"',
        "cat /proc/loadavg 2>/dev/null || true",
        'echo "@@meminfo"',
        "head -n 12 /proc/meminfo 2>/dev/null || true",
        'echo "@@df"',
        'df -Pk "$HOME" /tmp 2>/dev/null || true',
        'echo "@@gpu_tool"',
        "command -v nvidia-smi 2>/dev/null || true",
        'echo "@@gpu"',
        "nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,"
        "utilization.gpu,driver_version --format=csv,noheader,nounits 2>&1 || true",
        'echo "@@gpu_apps"',
        "nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory "
        "--format=csv,noheader,nounits 2>&1 || true",
        'echo "@@python"',
        "command -v python3 2>/dev/null || command -v python 2>/dev/null || true",
        'echo "@@slurm_tool"',
        "command -v sinfo 2>/dev/null || true",
    ]
    if include_slurm:
        lines += [
            'echo "@@sinfo"',
            f'sinfo -h -o "{parsers.SINFO_FORMAT}" 2>&1 || true',
            'echo "@@squeue"',
            f'squeue -h -u "$(whoami)" -o "{parsers.SQUEUE_FORMAT}" 2>&1 || true',
        ]
    lines.append('echo "@@end"')
    return "\n".join(lines)


@dataclass
class ProbeOutcome:
    snapshot: ResourceSnapshot
    sections: dict[str, str] = field(default_factory=dict)
    transcript: str = ""
    raw_errors: list[str] = field(default_factory=list)


class PosixRemoteProbe:
    def __init__(self, shell: PosixShell, node: NodeSpec, *, timeout: float | None = None) -> None:
        self.shell = shell
        self.node = node
        self.timeout = timeout or float(node.probe.timeout)

    def probe(self) -> ProbeOutcome:
        started = now_utc()
        script = build_probe_script(include_slurm=self.node.kind is NodeKind.SLURM)
        result = self.shell.run(script, timeout=self.timeout)
        if result.timed_out:
            raise CommandTimedOut(
                f"resource probe on node {self.node.id!r} timed out after {self.timeout:.0f}s",
                hint="raise probe.timeout or check the link quality",
            )
        sections = parsers.parse_probe_sections(result.stdout, marker=PROBE_MARKER)
        snapshot = assemble_snapshot(
            node=self.node,
            sections=sections,
            adapter="ssh",
            started=started,
            duration=result.duration_s,
            stderr=result.stderr,
        )
        return ProbeOutcome(
            snapshot=snapshot,
            sections=sections,
            transcript=result.stdout,
            raw_errors=[result.stderr.strip()] if result.stderr.strip() else [],
        )


def assemble_snapshot(
    *,
    node: NodeSpec,
    sections: dict[str, str],
    adapter: str,
    started: datetime,
    duration: float | None = None,
    stderr: str = "",
) -> ResourceSnapshot:
    checks: list[Check] = []
    errors: list[str] = []

    home_dir = sections.get("home", "").strip() or None
    sysname, release, machine = parsers.parse_uname(sections.get("host", ""))
    if sysname:
        checks.append(Check(name="host", status=ProbeStatus.OK, detail=f"{sysname} {release or ''} {machine or ''}".strip()))
    else:
        checks.append(Check(name="host", status=ProbeStatus.WARN, detail="uname produced no output"))

    cores = parsers.parse_cpu_count(sections.get("cpu", ""))
    cpu = parsers.parse_loadavg(sections.get("loadavg", ""))
    cpu.logical_cores = cores
    if cores:
        checks.append(Check(name="cpu", status=ProbeStatus.OK, detail=f"{cores} logical cores"))
    else:
        checks.append(Check(name="cpu", status=ProbeStatus.WARN, detail="CPU count unavailable"))

    memory = parsers.parse_meminfo(sections.get("meminfo", ""))
    if memory.total_mb:
        checks.append(
            Check(
                name="memory",
                status=ProbeStatus.OK,
                detail=f"{memory.total_mb} MB total, {memory.available_mb} MB available",
            )
        )
    else:
        checks.append(Check(name="memory", status=ProbeStatus.WARN, detail="/proc/meminfo unavailable"))

    disks = parsers.parse_df(sections.get("df", ""))
    if disks:
        smallest = min((d.avail_kb or 0) for d in disks) / 1024 / 1024
        checks.append(
            Check(
                name="disk",
                status=ProbeStatus.OK if smallest >= 5 else ProbeStatus.WARN,
                detail=f"{smallest:.1f} GiB free at the tightest mount",
                data={"mounts": [d.path for d in disks]},
            )
        )
    else:
        checks.append(Check(name="disk", status=ProbeStatus.WARN, detail="df produced no usable output"))

    gpu_tool = sections.get("gpu_tool", "").strip()
    gpus: list[GpuInfo] = []
    gpu_status = ProbeStatus.UNKNOWN
    if not gpu_tool:
        gpu_status = ProbeStatus.SKIPPED
        if node.kind is NodeKind.SLURM:
            # A login node normally has no GPU and no nvidia-smi. GPU capacity is
            # a property of the compute nodes and is reported by `sinfo %G`.
            gpu_partitions = [
                p.name
                for p in parsers.parse_sinfo(sections.get("sinfo", ""))
                if "gpu" in (p.gres or "").lower()
            ]
            checks.append(
                Check(
                    name="gpu",
                    status=ProbeStatus.SKIPPED,
                    detail=(
                        "login node has no nvidia-smi; GPU capacity comes from the "
                        f"scheduler ({', '.join(gpu_partitions) or 'no GPU partition reported'})"
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    name="gpu",
                    status=ProbeStatus.FAIL if node.gpus_expected else ProbeStatus.SKIPPED,
                    detail="nvidia-smi not found on this node",
                )
            )
    else:
        gpus = parsers.parse_nvidia_smi_gpus(sections.get("gpu", ""))
        if gpus:
            apps = parsers.parse_nvidia_smi_apps(sections.get("gpu_apps", ""))
            gpus = parsers.attach_processes(gpus, apps)
            gpu_status = ProbeStatus.OK
            busy = sum(1 for g in gpus if g.processes)
            checks.append(
                Check(
                    name="gpu",
                    status=ProbeStatus.OK,
                    detail=f"{len(gpus)} GPU(s), {busy} with active compute processes",
                    data={"driver": gpus[0].driver_version},
                )
            )
        else:
            gpu_status = ProbeStatus.WARN
            first = (sections.get("gpu", "").strip().splitlines() or [""])[0]
            checks.append(
                Check(
                    name="gpu",
                    status=ProbeStatus.WARN,
                    detail=f"nvidia-smi present but returned no GPUs: {first[:160]}",
                )
            )

    scheduler = SchedulerView()
    if node.kind is NodeKind.SLURM:
        slurm_tool = sections.get("slurm_tool", "").strip()
        sinfo_out = sections.get("sinfo", "")
        if not slurm_tool:
            scheduler = SchedulerView(available=False, reason="sinfo not found in PATH")
            checks.append(
                Check(
                    name="slurm",
                    status=ProbeStatus.FAIL,
                    detail="node is declared as slurm but sinfo is not available",
                )
            )
        else:
            partitions = parsers.parse_sinfo(sinfo_out)
            jobs = parsers.parse_squeue(sections.get("squeue", ""))
            scheduler = SchedulerView(
                available=bool(partitions) or "sinfo:" not in sinfo_out,
                partitions=partitions,
                user_jobs=jobs,
                reason=None if partitions else sinfo_out.strip()[:200],
            )
            checks.append(
                Check(
                    name="slurm",
                    status=ProbeStatus.OK if partitions else ProbeStatus.WARN,
                    detail=f"{len(partitions)} partition(s), {len(jobs)} job(s) for this user",
                )
            )

    python_path = sections.get("python", "").strip() or None
    if python_path:
        checks.append(Check(name="python", status=ProbeStatus.OK, detail=python_path))

    if stderr.strip():
        errors.append(stderr.strip()[:1000])

    return ResourceSnapshot(
        node_id=node.id,
        observed_at=started,
        adapter=adapter,
        home_dir=home_dir,
        cpu=cpu if (cpu.logical_cores or cpu.load_1m) else None,
        memory=memory if memory.total_mb else None,
        disks=disks,
        gpus=gpus,
        gpu_tool=gpu_status,
        scheduler=scheduler,
        checks=checks,
        errors=errors,
        duration_s=duration,
    )


# --------------------------------------------------------------------------- #
# Local probe
# --------------------------------------------------------------------------- #


class LocalProbe:
    def __init__(self, runner: Runner, node: NodeSpec, *, timeout: float | None = None) -> None:
        self.runner = runner
        self.node = node
        self.timeout = timeout or float(node.probe.timeout)

    def probe(self) -> ProbeOutcome:
        started = now_utc()
        checks: list[Check] = []
        host = f"{platform.system()} {platform.release()} {platform.machine()}"
        checks.append(Check(name="host", status=ProbeStatus.OK, detail=host))

        cpu = CpuInfo(logical_cores=os.cpu_count(), model=platform.processor() or None)
        if hasattr(os, "getloadavg"):  # POSIX only
            try:
                one, five, fifteen = os.getloadavg()
                cpu.load_1m, cpu.load_5m, cpu.load_15m = one, five, fifteen
            except OSError:  # pragma: no cover - unusual platforms
                pass
        checks.append(
            Check(
                name="cpu",
                status=ProbeStatus.OK if cpu.logical_cores else ProbeStatus.WARN,
                detail=f"{cpu.logical_cores} logical cores",
            )
        )

        memory = _local_memory()
        checks.append(
            Check(
                name="memory",
                status=ProbeStatus.OK if memory.total_mb else ProbeStatus.WARN,
                detail=f"{memory.total_mb} MB total, {memory.available_mb} MB available",
            )
        )

        disks = [_local_disk(Path.home())]
        checks.append(
            Check(
                name="disk",
                status=ProbeStatus.OK,
                detail=f"{disks[0].avail_kb / 1024 / 1024:.1f} GiB free in home",
            )
        )

        gpus: list[GpuInfo] = []
        gpu_status = ProbeStatus.UNKNOWN
        nvidia = shutil.which("nvidia-smi")
        if not nvidia:
            gpu_status = ProbeStatus.SKIPPED
            checks.append(
                Check(
                    name="gpu",
                    status=ProbeStatus.FAIL if self.node.gpus_expected else ProbeStatus.SKIPPED,
                    detail="nvidia-smi not installed on this host",
                )
            )
        else:
            query = self.runner.run(
                [
                    nvidia,
                    "--query-gpu=index,uuid,name,memory.total,memory.used,"
                    "utilization.gpu,driver_version",
                    "--format=csv,noheader,nounits",
                ],
                timeout=self.timeout,
            )
            gpus = parsers.parse_nvidia_smi_gpus(query.stdout)
            if gpus:
                apps = self.runner.run(
                    [
                        nvidia,
                        "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                        "--format=csv,noheader,nounits",
                    ],
                    timeout=self.timeout,
                )
                gpus = parsers.attach_processes(gpus, parsers.parse_nvidia_smi_apps(apps.stdout))
                gpu_status = ProbeStatus.OK
            else:
                gpu_status = ProbeStatus.WARN
            checks.append(
                Check(
                    name="gpu",
                    status=ProbeStatus.OK if gpus else ProbeStatus.WARN,
                    detail=f"{len(gpus)} GPU(s) via nvidia-smi",
                )
            )

        snapshot = ResourceSnapshot(
            node_id=self.node.id,
            observed_at=started,
            adapter="local",
            home_dir=str(Path.home()),
            cpu=cpu,
            memory=memory,
            disks=disks,
            gpus=gpus,
            gpu_tool=gpu_status,
            scheduler=SchedulerView(available=False, reason="local node has no scheduler"),
            checks=checks,
            errors=[],
            duration_s=(now_utc() - started).total_seconds(),
        )
        return ProbeOutcome(
            snapshot=snapshot,
            sections={"host": host},
            transcript=host,
        )


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _local_memory() -> MemoryInfo:
    if sys.platform == "win32":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            total = status.ullTotalPhys // 1024 // 1024
            available = status.ullAvailPhys // 1024 // 1024
            return MemoryInfo(total_mb=total, available_mb=available, used_mb=total - available)
        return MemoryInfo()
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        return parsers.parse_meminfo(meminfo.read_text(encoding="utf-8", errors="replace"))
    try:  # pragma: no cover - macOS fallback
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1024 // 1024
        available = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES") // 1024 // 1024
        return MemoryInfo(total_mb=total, available_mb=available, used_mb=total - available)
    except (ValueError, OSError):  # pragma: no cover
        return MemoryInfo()


def _local_disk(path: Path) -> DiskInfo:
    usage = shutil.disk_usage(path)
    return DiskInfo(
        path=str(path),
        total_kb=usage.total // 1024,
        used_kb=usage.used // 1024,
        avail_kb=usage.free // 1024,
    )


def utcnow() -> datetime:  # pragma: no cover - convenience for callers
    return datetime.now(UTC)
