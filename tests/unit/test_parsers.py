"""Parsers for /proc, nvidia-smi and Slurm output."""

from __future__ import annotations

from rcf.resources import parsers

MEMINFO = """\
MemTotal:       131072000 kB
MemFree:         20000000 kB
MemAvailable:    90000000 kB
Buffers:          1000000 kB
Cached:          30000000 kB
"""

DF = """\
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda1        102687672  40000000  57400000      42% /
/dev/sdb1       1000000000 999000000   1000000       1% /scratch
"""

GPU_CSV = """\
0, GPU-aaaa, NVIDIA A100-SXM4-40GB, 40960, 1024, 3, 535.104.05
1, GPU-bbbb, NVIDIA A100-SXM4-40GB, 40960, 20480, 71, 535.104.05
"""

APP_CSV = """\
GPU-bbbb, 4242, python, 20000
GPU-aaaa, 777, python, 512
"""

SINFO = """\
gpu*|up|1/3/0/4|8|64000|gpu:a100:4|1-00:00:00|node[001-004]
cpu|up|0/2/0/2|16|128000|(null)|2-00:00:00|node[005-006]
"""

SQUEUE = """\
1001|rcf-train|PENDING|gpu|1|8|gpu:1|0:00|(Priority)|Priority
1002|rcf-eval|RUNNING|gpu|1|4|gpu:1|01:02:03|node001|None
"""

SACCT = """\
1002|COMPLETED|0:0|00:10:00|2026-10-31T10:00:00|2026-10-31T10:10:00
1002.batch|COMPLETED|0:0|00:10:00|2026-10-31T10:00:00|2026-10-31T10:10:00
1002.extern|COMPLETED|0:0|00:10:00|2026-10-31T10:00:00|2026-10-31T10:10:00
"""


def test_meminfo_uses_available_not_free():
    memory = parsers.parse_meminfo(MEMINFO)
    assert memory.total_mb == 128000
    assert memory.available_mb == 87890  # 90,000,000 kB
    assert memory.used_mb == 128000 - 87890


def test_meminfo_without_memavailable_falls_back_to_free_plus_cache():
    text = "MemTotal: 1000000 kB\nMemFree: 400000 kB\nBuffers: 100000 kB\nCached: 100000 kB\n"
    memory = parsers.parse_meminfo(text)
    assert memory.available_mb == (400000 + 100000 + 100000) // 1024


def test_df_parses_every_mount():
    disks = parsers.parse_df(DF)
    assert [d.path for d in disks] == ["/", "/scratch"]
    assert disks[0].avail_kb == 57400000


def test_nvidia_smi_parses_gpu_and_processes_by_uuid():
    gpus = parsers.parse_nvidia_smi_gpus(GPU_CSV)
    assert len(gpus) == 2
    assert gpus[0].name == "NVIDIA A100-SXM4-40GB"
    assert gpus[1].memory_used_mb == 20480

    apps = parsers.parse_nvidia_smi_apps(APP_CSV)
    attached = parsers.attach_processes(gpus, apps)
    assert [p.pid for p in attached[0].processes] == [777]
    assert [p.pid for p in attached[1].processes] == [4242]


def test_nvidia_smi_tolerates_na_values():
    gpus = parsers.parse_nvidia_smi_gpus("0, N/A, Tesla T4, [N/A], [N/A], [N/A], N/A\n")
    assert gpus[0].uuid is None
    assert gpus[0].memory_total_mb is None
    assert gpus[0].driver_version is None


def test_sinfo_marks_default_partition_and_node_counts():
    partitions = parsers.parse_sinfo(SINFO)
    assert partitions[0].name == "gpu"
    assert partitions[0].total_nodes == 4
    assert partitions[0].avl_nodes == 3
    assert partitions[0].gres == "gpu:a100:4"
    assert partitions[1].gres is None


def test_squeue_extracts_gpu_count_and_state():
    jobs = parsers.parse_squeue(SQUEUE)
    assert jobs[0].state == "PENDING"
    assert jobs[0].gpus == 1
    assert jobs[1].state == "RUNNING"
    assert jobs[1].elapsed == "01:02:03"


def test_sacct_skips_step_rows():
    job = parsers.parse_sacct(SACCT, job_id="1002")
    assert job is not None
    assert job.state == "COMPLETED"
    assert parsers.parse_slurm_exit_code(job.exit_code) == 0


def test_sacct_normalises_cancelled_suffix():
    text = "1003|CANCELLED by 1000|0:15|00:00:02\n"
    job = parsers.parse_sacct(text)
    assert job is not None
    assert job.state == "CANCELLED"


def test_exit_code_signal_is_converted():
    assert parsers.parse_slurm_exit_code("0:15") == 143
    assert parsers.parse_slurm_exit_code("1:0") == 1
    assert parsers.parse_slurm_exit_code("None") is None


def test_probe_sections_parse_markers():
    transcript = "@@home\n/home/me\n@@cpu\n8\n@@end\n"
    sections = parsers.parse_probe_sections(transcript)
    assert sections == {"home": "/home/me", "cpu": "8"}


def test_key_value_block_ignores_noise():
    data = parsers.parse_key_value_block("noise\nstate=EXITED\nexit_code=0\nbad key=x\n")
    assert data == {"state": "EXITED", "exit_code": "0"}
