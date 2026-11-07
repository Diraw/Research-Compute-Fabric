"""Parsers against output captured from a real CentOS 7 / Slurm 20.11 cluster.

Captured 2026-11-04; node names and job ids are redacted, but the raw field
layout is unchanged, including the ``257437+`` memory suffix, ``(null)`` GRES
and ``infinite`` time limits.
"""

from __future__ import annotations

from pathlib import Path

from rcf.resources import parsers

FIXTURES = Path(__file__).parent.parent / "fixtures" / "slurm"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_real_sinfo_preserves_partition_fields():
    partitions = parsers.parse_sinfo(read("sinfo_centos7.txt"))
    by_name = {partition.name: partition for partition in partitions}

    assert set(by_name) == {"debug", "normal", "operation", "gpu", "temp"}
    debug = by_name["debug"]
    assert debug.total_nodes == 18
    assert debug.avl_nodes == 8
    assert debug.cpus == 64
    assert debug.memory_mb == 257437
    assert debug.gres is None
    assert debug.time_limit == "infinite"
    assert parsers.parse_slurm_time_limit_minutes(debug.time_limit) is None

    gpu = by_name["gpu"]
    assert gpu.avl_nodes == 2
    assert gpu.total_nodes == 2
    assert gpu.gres == "gpu:NVIDIAGeForceRTX3090:8"


def test_sinfo_availability_uses_idle_not_total_minus_allocated():
    # 4 allocated, 5 idle, 7 other, 16 total: "other" nodes are not schedulable.
    text = "mixed|up|4/5/7/16|32|64000|(null)|1:00:00|node[1-16]\n"

    partition = parsers.parse_sinfo(text)[0]

    assert partition.total_nodes == 16
    assert partition.avl_nodes == 5
    assert parsers.parse_slurm_time_limit_minutes(partition.time_limit) == 60.0


def test_real_sacct_skips_step_rows_and_reads_the_exit_code():
    job = parsers.parse_sacct(read("sacct_completed.txt"), job_id="123456")

    assert job is not None
    assert job.job_id == "123456"
    assert job.state == "COMPLETED"
    assert parsers.parse_slurm_exit_code(job.exit_code) == 0


def test_real_sacct_failed_job_keeps_the_command_not_found_code():
    job = parsers.parse_sacct(read("sacct_failed.txt"), job_id="123457")

    assert job is not None
    assert job.state == "FAILED"
    assert parsers.parse_slurm_exit_code(job.exit_code) == 127


def test_cancelled_job_takes_its_signal_from_the_batch_step():
    # Captured from a real `scancel` on 2026-11-07: the allocation row reports
    # 0:0 even though a signal stopped the work.
    text = (
        "267660|CANCELLED by 2038|0:0|00:00:14\n"
        "267660.batch|CANCELLED|0:15|00:00:15\n"
        "267660.extern|COMPLETED|0:0|00:00:14\n"
    )

    allocation = parsers.parse_sacct(text, job_id="267660")
    assert allocation is not None and allocation.exit_code == "0:0"
    assert parsers.parse_sacct_step_exit(text, job_id="267660") == 143


def test_cancelled_before_it_ran_has_no_batch_step():
    text = "267662|CANCELLED by 2038|0:0|00:00:00\n"

    assert parsers.parse_sacct_step_exit(text, job_id="267662") is None
