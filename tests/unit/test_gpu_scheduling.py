"""GPU schedulability: a desktop session is not an exclusive compute job."""

from __future__ import annotations

from conftest import make_home

from rcf.core.config import Config
from rcf.core.ids import now_utc
from rcf.core.models import (
    GpuInfo,
    GpuProcess,
    GpuSchedulabilityStatus,
    NodeKind,
    NodeSpec,
    ResourceSnapshot,
)
from rcf.core.runner import ScriptedRunner
from rcf.resources.inspector import ResourceInspector


def inspector(tmp_path) -> ResourceInspector:
    return ResourceInspector(Config.load(make_home(tmp_path)), ScriptedRunner())


def node() -> NodeSpec:
    return NodeSpec(
        id="ws",
        kind=NodeKind.SSH,
        ssh_alias="ws",
        gpus_expected=True,
    )


def snapshot(
    *processes: GpuProcess,
    used_mb: int | None = 1548,
    total_mb: int | None = 24564,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        node_id="ws",
        observed_at=now_utc(),
        adapter="test",
        gpus=[
            GpuInfo(
                index=0,
                uuid="GPU-0",
                name="NVIDIA GeForce RTX 4090",
                memory_total_mb=total_mb,
                memory_used_mb=used_mb,
                processes=list(processes),
            )
        ],
    )


def display_process() -> GpuProcess:
    return GpuProcess(
        pid=623095,
        name="/usr/libexec/gnome-remote-desktop-daemon",
        used_memory_mb=392,
        process_type="C+G",
    )


def test_display_only_usage_is_available_and_selectable(tmp_path):
    snap = snapshot(display_process())
    assessment = inspector(tmp_path).assess_gpus(node(), snap)[0]

    assert assessment.status is GpuSchedulabilityStatus.AVAILABLE
    assert "display/session process" in assessment.reasons[0]
    assert assessment.evidence["display_only"] is True
    assert assessment.evidence["processes"][0]["process_type"] == "C+G"

    selected, reasons = inspector(tmp_path).select_gpus(node(), snap, [assessment], 1)
    assert [s.gpu_index for s in selected] == [0]
    assert reasons == []


def test_large_display_usage_needs_explicit_vram_evidence(tmp_path):
    snap = snapshot(display_process(), used_mb=12000)
    assessment = inspector(tmp_path).assess_gpus(node(), snap)[0]

    assert assessment.status is GpuSchedulabilityStatus.UNKNOWN
    assert "pass gpu_memory_gb" in assessment.reasons[0]

    selected, reasons = inspector(tmp_path).select_gpus(node(), snap, [assessment], 1)
    assert selected == [] and reasons

    selected, reasons = inspector(tmp_path).select_gpus(
        node(), snap, [assessment], 1, required_vram_mb=8000
    )
    assert [s.gpu_index for s in selected] == [0]
    assert reasons == []


def test_real_compute_process_still_occupies_the_device(tmp_path):
    real = GpuProcess(
        pid=100, name="/usr/bin/python3", used_memory_mb=8000, process_type="C"
    )
    snap = snapshot(display_process(), real, used_mb=9548)
    assessment = inspector(tmp_path).assess_gpus(node(), snap)[0]

    assert assessment.status is GpuSchedulabilityStatus.OCCUPIED
    assert "1 compute process(es) active" in assessment.reasons[0]
    assert "1 display/session process(es) also present" in assessment.reasons[1]

    selected, _ = inspector(tmp_path).select_gpus(node(), snap, [assessment], 1)
    assert selected == []

    selected, _ = inspector(tmp_path).select_gpus(
        node(), snap, [assessment], 1, required_vram_mb=8000, allow_shared=True
    )
    assert [s.gpu_index for s in selected] == [0]


def test_display_classification_works_without_pmon(tmp_path):
    # Older drivers may not support `pmon`; the executable name is enough.
    proc = GpuProcess(
        pid=7, name="/usr/bin/gnome-remote-desktop-daemon", used_memory_mb=392,
        process_type=None,
    )
    assessment = inspector(tmp_path).assess_gpus(node(), snapshot(proc, used_mb=1548))[0]
    assert assessment.status is GpuSchedulabilityStatus.AVAILABLE


def test_a_compute_named_like_a_desktop_app_is_still_compute(tmp_path):
    # Type C+G alone is not enough: an unknown executable keeps the conservative
    # verdict, so a real job cannot be waved through by a name pattern.
    proc = GpuProcess(pid=8, name="/home/me/custom-cuda-job", used_memory_mb=9000,
                      process_type="C+G")
    assessment = inspector(tmp_path).assess_gpus(node(), snapshot(proc, used_mb=9000))[0]
    assert assessment.status is GpuSchedulabilityStatus.OCCUPIED
