"""Fake ``nvidia-smi`` with two GPUs: one idle, one busy.

Output is shaped exactly like the real ``--query-gpu`` / ``--query-compute-apps``
CSV so the production parsers are exercised, not bypassed.
"""

from __future__ import annotations

import os
import sys

GPU_QUERY = [
    "0, GPU-11111111-1111-1111-1111-111111111111, NVIDIA A100-SXM4-40GB, 40960, 1024, 3, 535.104.05",
    "1, GPU-22222222-2222-2222-2222-222222222222, NVIDIA A100-SXM4-40GB, 40960, 20480, 71, 535.104.05",
]

APP_QUERY = [
    "GPU-22222222-2222-2222-2222-222222222222, 4242, python, 20000",
]


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_GPU") == "none":
        sys.stderr.write("bash: nvidia-smi: command not found\n")
        return 127
    joined = " ".join(argv)
    if "--query-compute-apps" in joined:
        sys.stdout.write("\n".join(APP_QUERY) + "\n")
        return 0
    if "--query-gpu" in joined:
        lines = GPU_QUERY
        if os.environ.get("FAKE_GPU") == "busy":
            lines = [GPU_QUERY[0].replace(", 1024, 3,", ", 38000, 99,")]
        sys.stdout.write("\n".join(lines) + "\n")
        return 0
    sys.stdout.write("fake nvidia-smi: unsupported invocation\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
