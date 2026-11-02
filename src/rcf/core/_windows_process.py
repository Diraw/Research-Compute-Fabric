"""Bounded Windows command execution, separate from persistent local jobs.

Anonymous capture pipes cannot enforce a timeout: a grandchild may hold the
write end after its parent dies. File-backed stdio never waits for pipe EOF.
A private Job Object owns the tree by handle, not by recyclable PIDs, and the
root is assigned while suspended so it cannot spawn before being contained.
"""
from __future__ import annotations

import ctypes
import math
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from ctypes import wintypes


def run_managed(
    args: Sequence[str], *, payload: bytes | None, timeout: float,
    cwd: str | None, env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one owned Windows tree; never use this for ``spawn_detached``."""
    import _winapi
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_ops", "write_ops", "other_ops", "read", "write", "other")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = thread = None
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output, \
                tempfile.TemporaryFile() as error:
            if payload is not None:
                input_file.write(payload)
                input_file.seek(0)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
            handles = [msvcrt.get_osfhandle(stream.fileno())
                       for stream in (input_file, output, error)]
            startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
            # Duplicate only our handles as inheritable. Mutating the original
            # file handles would race other threads launching children.
            duplicates = [_winapi.DuplicateHandle(_winapi.GetCurrentProcess(), handle,
                          _winapi.GetCurrentProcess(), 0, True, _winapi.DUPLICATE_SAME_ACCESS)
                          for handle in handles]
            try:
                startup.hStdInput, startup.hStdOutput, startup.hStdError = duplicates
                startup.lpAttributeList = {"handle_list": duplicates}
                process, thread, _, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(list(args)), None, None, True,
                    0x08000000 | 0x00000004 | 0x00080000,
                    dict(env) if env is not None else None, os.fspath(cwd) if cwd is not None else None, startup,
                )  # CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT
            finally:
                for handle in duplicates:
                    _winapi.CloseHandle(handle)
            if not kernel.AssignProcessToJobObject(job, process):
                # Fail closed: never resume a root outside its owned job.
                raise ctypes.WinError(ctypes.get_last_error())
            if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            _winapi.CloseHandle(thread)
            thread = None
            milliseconds = min(0xFFFFFFFE, max(0, math.ceil(timeout * 1000)))
            timed_out = _winapi.WaitForSingleObject(process, milliseconds) == _winapi.WAIT_TIMEOUT
            code = 124 if timed_out else _winapi.GetExitCodeProcess(process)
            if timed_out:
                kernel.TerminateJobObject(job, 124)
                _winapi.WaitForSingleObject(process, 100)  # bounded, never communicate()
            else:
                # SSH/dispatch helpers may legitimately start detached jobs.
                # Closing a successfully completed command must not kill them.
                limits.basic.flags = 0
                if not kernel.SetInformationJobObject(
                    job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            output.seek(0)
            error.seek(0)
            stdout = output.read(os.fstat(output.fileno()).st_size)
            stderr = error.read(os.fstat(error.fileno()).st_size)
            if timed_out:
                raise subprocess.TimeoutExpired(list(args), timeout, output=stdout, stderr=stderr)
            return subprocess.CompletedProcess(list(args), code, stdout, stderr)
    finally:
        # Handles remain valid even after the process exits: no PID lookup or
        # taskkill /T races, and no possibility of targeting an unrelated PID.
        if process is not None:
            if _winapi.GetExitCodeProcess(process) == 259:
                _winapi.TerminateProcess(process, 124)
            _winapi.CloseHandle(process)
        if thread is not None:
            _winapi.CloseHandle(thread)
        kernel.CloseHandle(job)
