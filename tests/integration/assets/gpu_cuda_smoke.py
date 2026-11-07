"""Prove a Slurm GPU allocation can really reach CUDA, with no extra installs.

This is deliberately a *driver* smoke test: it loads ``libcuda.so.1`` through
``ctypes``, creates a context, allocates device memory and round-trips bytes.  It
does not compile or run a kernel, and it does not need a CUDA toolkit, cupy or
torch — those are separate claims.  What it does prove is the part that silently
fails on a badly configured cluster: the allocation actually exposes a device and
the driver accepts work inside the batch step.

Writes ``gpu_cuda_smoke.json`` into ``$RCF_OUTPUT_DIR`` (or the working directory)
and exits 0 only when the round trip matched.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

SIZE = 64 * 1024 * 1024  # 64 MiB: enough to prove a real allocation, fast to copy
STRIDE = 4096


def _bind(cuda: ctypes.CDLL) -> None:
    """ctypes defaults to int arguments, which truncates 64-bit pointers."""

    cuda.cuInit.argtypes = [ctypes.c_uint]
    cuda.cuInit.restype = ctypes.c_int
    cuda.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    cuda.cuDeviceGetCount.restype = ctypes.c_int
    cuda.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    cuda.cuDeviceGet.restype = ctypes.c_int
    cuda.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    cuda.cuDeviceGetName.restype = ctypes.c_int
    cuda.cuDeviceTotalMem_v2.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
    cuda.cuDeviceTotalMem_v2.restype = ctypes.c_int
    cuda.cuCtxCreate_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_int]
    cuda.cuCtxCreate_v2.restype = ctypes.c_int
    cuda.cuMemAlloc_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
    cuda.cuMemAlloc_v2.restype = ctypes.c_int
    cuda.cuMemcpyHtoD_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    cuda.cuMemcpyHtoD_v2.restype = ctypes.c_int
    cuda.cuMemcpyDtoH_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    cuda.cuMemcpyDtoH_v2.restype = ctypes.c_int
    cuda.cuMemFree_v2.argtypes = [ctypes.c_void_p]
    cuda.cuMemFree_v2.restype = ctypes.c_int
    cuda.cuCtxDestroy_v2.argtypes = [ctypes.c_void_p]
    cuda.cuCtxDestroy_v2.restype = ctypes.c_int


def _check(cuda: ctypes.CDLL, code: int, step: str) -> None:
    if code == 0:
        return
    name = ctypes.c_char_p()
    cuda.cuGetErrorName.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
    cuda.cuGetErrorName(code, ctypes.byref(name))
    raise RuntimeError(f"{step} failed: {name.value.decode() if name.value else code}")


def run() -> dict[str, object]:
    result: dict[str, object] = {
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "slurm_job_gpus": os.environ.get("SLURM_JOB_GPUS"),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "ok": False,
    }
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError as exc:
        result["error"] = f"libcuda.so.1 not loadable: {exc}"
        return result
    _bind(cuda)

    _check(cuda, cuda.cuInit(0), "cuInit")
    count = ctypes.c_int()
    _check(cuda, cuda.cuDeviceGetCount(ctypes.byref(count)), "cuDeviceGetCount")
    result["device_count"] = count.value
    if count.value < 1:
        result["error"] = "no CUDA device visible in this allocation"
        return result

    device = ctypes.c_int()
    _check(cuda, cuda.cuDeviceGet(ctypes.byref(device), 0), "cuDeviceGet")
    name = ctypes.create_string_buffer(128)
    _check(cuda, cuda.cuDeviceGetName(name, len(name), device), "cuDeviceGetName")
    total = ctypes.c_size_t()
    _check(cuda, cuda.cuDeviceTotalMem_v2(ctypes.byref(total), device), "cuDeviceTotalMem")
    result["device_name"] = name.value.decode()
    result["device_total_mb"] = round(total.value / (1024 * 1024), 1)

    context = ctypes.c_void_p()
    _check(cuda, cuda.cuCtxCreate_v2(ctypes.byref(context), 0, device), "cuCtxCreate")
    pointer = ctypes.c_void_p()
    try:
        _check(cuda, cuda.cuMemAlloc_v2(ctypes.byref(pointer), SIZE), "cuMemAlloc")
        result["allocated_mb"] = SIZE // (1024 * 1024)
        host = (ctypes.c_ubyte * SIZE)()
        for offset in range(0, SIZE, STRIDE):
            host[offset] = (offset // STRIDE) & 0xFF
        _check(cuda, cuda.cuMemcpyHtoD_v2(pointer, host, SIZE), "cuMemcpyHtoD")
        back = (ctypes.c_ubyte * SIZE)()
        _check(cuda, cuda.cuMemcpyDtoH_v2(back, pointer, SIZE), "cuMemcpyDtoH")
        mismatches = sum(
            1 for offset in range(0, SIZE, STRIDE) if back[offset] != host[offset]
        )
        result["copied_mb"] = SIZE // (1024 * 1024)
        result["mismatches"] = mismatches
        result["ok"] = mismatches == 0
    finally:
        if pointer.value:
            cuda.cuMemFree_v2(pointer)
        cuda.cuCtxDestroy_v2(context)
    return result


def main() -> int:
    result = run()
    target = Path(os.environ.get("RCF_OUTPUT_DIR") or ".")
    target.mkdir(parents=True, exist_ok=True)
    (target / "gpu_cuda_smoke.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    if not result["ok"]:
        print(f"FAILED: {result.get('error', 'byte round trip differed')}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
