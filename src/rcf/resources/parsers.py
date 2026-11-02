"""Parsers for POSIX / nvidia-smi / Slurm command output.

All parsers are pure functions over text so they can be tested against output
captured from real clusters (see ``tests/fixtures``).  They are deliberately
forgiving about extra columns and missing values: an old CentOS node, a
partition-less ``sinfo`` and a ``sacct`` without accounting all have to degrade
to ``unknown`` instead of crashing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..core.models import (
    CpuInfo,
    DiskInfo,
    GpuInfo,
    GpuProcess,
    MemoryInfo,
    SlurmJob,
    SlurmPartition,
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    match = _NUM_RE.search(text.replace(",", ""))
    return float(match.group()) if match else None


def _int(text: str | None) -> int | None:
    value = _num(text)
    return int(value) if value is not None else None


# --------------------------------------------------------------------------- #
# host / cpu / memory / disk
# --------------------------------------------------------------------------- #


def parse_uname(text: str) -> tuple[str | None, str | None, str | None]:
    """``uname -srm`` → (sysname, release, machine)."""

    parts = text.strip().split()
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    if len(parts) == 1:
        return parts[0], None, None
    return None, None, None


def parse_cpu_count(text: str) -> int | None:
    return _int(text.strip().splitlines()[0]) if text.strip() else None


def parse_loadavg(text: str) -> CpuInfo:
    fields = text.strip().split()
    if len(fields) < 3:
        return CpuInfo()
    return CpuInfo(
        load_1m=_num(fields[0]),
        load_5m=_num(fields[1]),
        load_15m=_num(fields[2]),
    )


def parse_meminfo(text: str) -> MemoryInfo:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if not rest:
            continue
        kb = _int(rest)
        if kb is not None:
            values[key.strip()] = kb
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        free = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
        available = free if values else None
    total_mb = total // 1024 if total else None
    available_mb = available // 1024 if available is not None else None
    used_mb = (
        total_mb - available_mb
        if total_mb is not None and available_mb is not None
        else None
    )
    return MemoryInfo(total_mb=total_mb, available_mb=available_mb, used_mb=used_mb)


def parse_df(text: str) -> list[DiskInfo]:
    """Parse ``df -Pk`` output (header optional)."""

    disks: list[DiskInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("filesystem"):
            continue
        fields = line.split()
        if len(fields) < 6:
            continue
        # -P guarantees one record per line: fs, 1k-blocks, used, avail, capacity, mount
        mount = " ".join(fields[5:])
        disks.append(
            DiskInfo(
                path=mount,
                total_kb=_int(fields[1]),
                used_kb=_int(fields[2]),
                avail_kb=_int(fields[3]),
            )
        )
    return disks


# --------------------------------------------------------------------------- #
# GPU
# --------------------------------------------------------------------------- #

_GPU_QUERY_FIELDS = (
    "index",
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "utilization.gpu",
    "driver_version",
)


def parse_nvidia_smi_gpus(text: str) -> list[GpuInfo]:
    """Parse ``--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,driver_version``."""

    gpus: list[GpuInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("index"):
            continue
        # Names may contain commas ("NVIDIA A100-SXM4-40GB") only in rare OEM
        # strings, so split from the right for the numeric tail.
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        index = _int(parts[0])
        if index is None:
            continue
        gpus.append(
            GpuInfo(
                index=index,
                uuid=None if parts[1] in {"", "N/A", "[N/A]"} else parts[1],
                name=parts[2] or None,
                memory_total_mb=_int(parts[3]),
                memory_used_mb=_int(parts[4]),
                utilization_pct=_int(parts[5]),
                driver_version=None if parts[6] in {"", "N/A", "[N/A]"} else parts[6],
            )
        )
    return gpus


def parse_nvidia_smi_apps(text: str) -> list[GpuProcess]:
    """Parse ``--query-compute-apps=gpu_uuid,pid,process_name,used_memory``."""

    processes: list[GpuProcess] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("gpu_uuid"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        processes.append(
            GpuProcess(
                gpu_uuid=None if parts[0] in {"", "N/A"} else parts[0],
                pid=_int(parts[1]),
                name=parts[2] or None,
                used_memory_mb=_int(parts[3]),
            )
        )
    return processes


def attach_processes(gpus: Iterable[GpuInfo], processes: Iterable[GpuProcess]) -> list[GpuInfo]:
    """Attach compute processes to their GPU by uuid (fallback: single GPU)."""

    gpu_list = list(gpus)
    by_uuid = {g.uuid: g for g in gpu_list if g.uuid}
    for process in processes:
        target = by_uuid.get(process.gpu_uuid) if process.gpu_uuid else None
        if target is None and len(gpu_list) == 1:
            target = gpu_list[0]
        if target is not None:
            target.processes.append(process)
    return gpu_list


# --------------------------------------------------------------------------- #
# Slurm
# --------------------------------------------------------------------------- #

SINFO_FORMAT = "%P|%a|%F|%c|%m|%G|%l|%N"
SQUEUE_FORMAT = "%i|%j|%T|%P|%D|%C|%b|%M|%N|%R"
SACCT_FORMAT = "JobID,State,ExitCode,Elapsed,Start,End"

_SLURM_STATE_ALIASES = {
    "R": "RUNNING",
    "PD": "PENDING",
    "CG": "COMPLETING",
    "CD": "COMPLETED",
    "F": "FAILED",
    "CA": "CANCELLED",
    "TO": "TIMEOUT",
    "NF": "NODE_FAIL",
    "OOM": "OUT_OF_MEMORY",
}


def _partition_nodes(field: str) -> tuple[int | None, int | None]:
    """``4/12/0/16`` → (allocated=4, total=16); unknown → (None, None)."""

    match = re.fullmatch(r"(\d+)/(\d+)/(\d+)/(\d+)", field.strip())
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(4))


def parse_sinfo(text: str) -> list[SlurmPartition]:
    partitions: list[SlurmPartition] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("PARTITION"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 7:
            continue
        name = parts[0].rstrip("*").strip()
        allocated, total = _partition_nodes(parts[2])
        partitions.append(
            SlurmPartition(
                name=name,
                state=parts[1] or None,
                avl_nodes=(total - allocated) if total is not None and allocated is not None else None,
                total_nodes=total,
                cpus=_int(parts[3]),
                memory_mb=_int(parts[4]),
                gres=None if parts[5] in {"", "(null)", "N/A"} else parts[5],
                time_limit=None if parts[6] in {"", "N/A"} else parts[6],
            )
        )
    return partitions


def _gpus_from_tres(field: str) -> int | None:
    if not field:
        return None
    match = re.search(r"gpu(?::[A-Za-z0-9_-]+)?:(\d+)", field)
    if match:
        return int(match.group(1))
    if field.strip().isdigit():
        return int(field.strip())
    return None


def _normalize_state(raw: str) -> str:
    state = raw.strip()
    for suffix in (" by ", "+"):
        if suffix in state:
            state = state.split(suffix)[0]
    state = state.upper()
    return _SLURM_STATE_ALIASES.get(state, state)


def parse_squeue(text: str) -> list[SlurmJob]:
    jobs: list[SlurmJob] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("JOBID"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue
        jobs.append(
            SlurmJob(
                job_id=parts[0],
                name=parts[1] or None,
                state=_normalize_state(parts[2]),
                partition=parts[3] or None,
                nodes=_int(parts[4]),
                cpus=_int(parts[5]),
                gpus=_gpus_from_tres(parts[6]),
                elapsed=parts[7] or None,
                node_list=parts[8] if len(parts) > 8 else None,
                reason=parts[9] if len(parts) > 9 else None,
            )
        )
    return jobs


def parse_sacct(text: str, *, job_id: str | None = None) -> SlurmJob | None:
    """Parse ``sacct -P -n -o JobID,State,ExitCode,Elapsed,Start,End``.

    Slurm emits one row for the allocation plus one row per step
    (``123.batch``, ``123.extern``).  Only the allocation row is meaningful for
    the Run state, so step rows are skipped.
    """

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        raw_id = parts[0]
        if "." in raw_id or raw_id.endswith("+"):
            continue
        if job_id is not None and raw_id != job_id:
            continue
        return SlurmJob(
            job_id=raw_id,
            state=_normalize_state(parts[1]),
            exit_code=parts[2] or None,
            elapsed=parts[3] if len(parts) > 3 else None,
        )
    return None


def parse_slurm_exit_code(raw: str | None) -> int | None:
    """``0:0`` → 0; ``1:0`` → 1; ``0:15`` → 15 (signal terminated)."""

    if not raw or raw in {"", "None", "N/A"}:
        return None
    main, _, signal = raw.partition(":")
    code = _int(main)
    if code is None:
        return None
    if code == 0 and signal:
        signal_code = _int(signal)
        if signal_code:
            return 128 + signal_code
    return code


# --------------------------------------------------------------------------- #
# Remote status probe
# --------------------------------------------------------------------------- #


def parse_key_value_block(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines emitted by the remote status probe."""

    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            data[key] = value.strip()
    return data


def parse_probe_sections(text: str, *, marker: str = "@@") -> dict[str, str]:
    """Split a batched probe transcript into ``{section: body}``."""

    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            if current is not None:
                sections[current] = "\n".join(lines).strip("\n")
            name = stripped[len(marker) :].strip()
            if name == "end":
                current = None
                lines = []
                continue
            current = name
            lines = []
            continue
        if current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip("\n")
    return sections
