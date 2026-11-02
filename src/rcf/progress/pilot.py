"""Generate the dependency-free Python 3.6 pilot deadline supervisor.

Only the generated runtime script launches processes (the same exception as
executor supervisors). Controller-side code only serializes reviewed commands.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.errors import ExecutorError
from ..core.models import PlannedCommand
from ..core.runner import shquote

PILOT_SCRIPT = r'''"""RCF pilot watchdog; standalone Python 3.6+, no RCF installation needed."""
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import traceback


def write(run_dir, name, value):
    path = os.path.join(run_dir, name)
    with open(path + ".pilot.tmp", "w", encoding="utf-8") as handle:
        handle.write(value)
    os.replace(path + ".pilot.tmp", path)


class WindowsTree:
    """A suspended launch assigned to a Job before it can create descendants."""

    def __init__(self, argv, cwd, env, log, shell):
        import ctypes
        import msvcrt
        import ctypes.wintypes
        w = ctypes.wintypes

        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.api
        class Startup(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR),
                        ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                        ("x", w.DWORD), ("y", w.DWORD), ("xs", w.DWORD),
                        ("ys", w.DWORD), ("xc", w.DWORD), ("yc", w.DWORD),
                        ("fill", w.DWORD), ("flags", w.DWORD), ("show", w.WORD),
                        ("reserved_size", w.WORD), ("reserved_bytes", ctypes.c_void_p),
                        ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]
        class Process(ctypes.Structure):
            _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE),
                        ("pid", w.DWORD), ("tid", w.DWORD)]
        class Accounting(ctypes.Structure):
            _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
                        ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
                        ("faults", w.DWORD), ("total", w.DWORD), ("active", w.DWORD),
                        ("terminated", w.DWORD)]
        self.Accounting = Accounting
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.AssignProcessToJobObject.restype = w.BOOL
        api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                w.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = w.BOOL
        api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        api.TerminateJobObject.restype = w.BOOL
        api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        api.TerminateProcess.restype = w.BOOL
        api.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        api.GetExitCodeProcess.restype = w.BOOL
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.WaitForSingleObject.restype = w.DWORD
        api.ResumeThread.argtypes = [w.HANDLE]
        api.ResumeThread.restype = w.DWORD
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        api.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p,
                                       ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p,
                                       w.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Process)]
        api.CreateProcessW.restype = w.BOOL
        self.job = api.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        self.process = None
        info = Process()
        # Explicit inheritable handles keep output in run.log with no console.
        with open(os.devnull, "rb") as null:
            handles = [msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(log.fileno())]
            inherited = [os.get_handle_inheritable(handle) for handle in handles]
            try:
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                startup = Startup()
                startup.cb = ctypes.sizeof(startup)
                startup.flags = 0x100  # STARTF_USESTDHANDLES
                startup.stdin, startup.stdout, startup.stderr = handles[0], handles[1], handles[1]
                command = subprocess.list2cmdline(argv)
                if shell:
                    command = subprocess.list2cmdline([env.get("COMSPEC", "cmd.exe")]) + ' /c "' + command + '"'
                block = ctypes.create_unicode_buffer("\0".join(
                    key + "=" + value for key, value in sorted(env.items())) + "\0\0")
                if not api.CreateProcessW(None, ctypes.create_unicode_buffer(command), None, None,
                                          True, 0x08000000 | 0x4 | 0x400, block, cwd,
                                          ctypes.byref(startup), ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.process, self.pid = info.process, info.pid
                if not api.AssignProcessToJobObject(self.job, self.process):
                    raise ctypes.WinError(ctypes.get_last_error())
                if api.ResumeThread(info.thread) == 0xffffffff:
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                if self.process:
                    api.TerminateProcess(self.process, 126)
                    api.WaitForSingleObject(self.process, 5000)
                self.close()
                raise
            finally:
                if info.thread:
                    api.CloseHandle(info.thread)
                for handle, old in zip(handles, inherited):
                    os.set_handle_inheritable(handle, old)

    def poll(self):
        w = self.ctypes.wintypes
        if self.api.WaitForSingleObject(self.process, 0) == 0x102:
            return None
        code = w.DWORD()
        if not self.api.GetExitCodeProcess(self.process, self.ctypes.byref(code)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return code.value

    def active(self):
        info = self.Accounting()
        if not self.api.QueryInformationJobObject(self.job, 1, self.ctypes.byref(info),
                                                 self.ctypes.sizeof(info), None):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return info.active > 0

    def kill(self):
        if not self.api.TerminateJobObject(self.job, 124):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def wait(self):
        until = time.monotonic() + 5
        self.api.WaitForSingleObject(self.process, 5000)
        while self.active() and time.monotonic() < until:
            time.sleep(0.01)
        if self.active():
            raise RuntimeError("pilot process tree termination is unconfirmed")
        return self.poll()

    def close(self):
        if self.process:
            self.api.CloseHandle(self.process)
            self.process = None
        if self.job:
            self.api.CloseHandle(self.job)
            self.job = None


def tree_active(proc):
    if os.name == "nt":
        return proc.active()
    if proc.poll() is None:
        return True
    # A dead launcher does not mean its descendants are done. Ignore zombies:
    # container PID 1 may not reap orphaned descendants, but they cannot execute.
    try:
        result = subprocess.run(["ps", "-eo", "pgid=,stat="], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, universal_newlines=True, timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode:
        return True  # Fail closed: only the deadline may finish unknown trees.
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == str(proc.pid) and not fields[1].startswith("Z"):
            return True
    return False


def stop_tree(proc):
    if os.name == "nt":
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        # Reap the leader while waiting: macOS returns EPERM when signalling a
        # process group whose leader is an unreaped zombie. Polling keeps the
        # pgid usable for surviving descendants.
        until = time.monotonic() + 0.5
        while time.monotonic() < until:
            proc.poll()
            time.sleep(0.025)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # If the group is already gone, reaping is still required; never
            # let a cleanup race replace the real exit code with a traceback.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    proc.wait()


def main():
    run_dir = os.path.dirname(os.path.abspath(__file__))
    with open(sys.argv[1], encoding="utf-8") as handle:
        spec = json.load(handle)
    timeout = spec["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError("pilot timeout must be an integer in 1..300")
    start = time.monotonic()
    interrupted = [0]
    def receive(signum, frame):
        interrupted[0] = signum
    signal.signal(signal.SIGTERM, receive)
    signal.signal(signal.SIGINT, receive)
    proc = None
    timed_out = False
    code = 126
    try:
        env = dict(os.environ)
        env.update(spec.get("env") or {})
        # A bounded legacy smoke run gets the same hard deadline but is not a
        # calibration pilot; never relabel it as one.
        if spec.get("calibration_pilot", True):
            env["RCF_PROGRESS_PILOT"] = "1"
        env.setdefault("RCF_PROGRESS_FILE", os.path.join(run_dir, "progress.jsonl"))
        argv = list(spec["argv"])
        # On Windows PATH may resolve a WSL launcher, not inherited Git Bash.
        bash = os.environ.get("SHELL", "bash") if os.name == "nt" else "bash"
        if os.path.basename(bash).lower() not in ("bash", "bash.exe"):
            bash = "bash"
        shell = bool(spec.get("shell"))
        if spec.get("posix_shell") and shell:
            argv, shell = [bash, "-c", " ".join(argv)], False
        setup = spec.get("preamble") or []
        if setup:
            # The preamble is already approved shell code; command arguments
            # remain positional data, never interpolated into that shell code.
            text = "{\n" + "\n".join(setup) + "\n} > " + shlex.quote(
                os.path.join(run_dir, "env.log")) + " 2>&1\n"
            for key, value in sorted((spec.get("env") or {}).items()):
                text += "export " + key + "=" + shlex.quote(value) + "\n"
            if shell:
                argv, shell = [bash, "-c", " ".join(argv)], False
            argv = [bash, "-c", text + 'exec "$@"', "rcf-pilot"] + argv
        else:
            write(run_dir, "env.log", "no environment setup\n")
        with open(os.path.join(run_dir, "run.log"), "w", encoding="utf-8", errors="replace") as log:
            log.write("# command\n%s\n# workdir\n%s\n" % (
                " ".join(spec["argv"]), spec["workdir"]))
            log.flush()
            if os.name == "nt":
                proc = WindowsTree(argv, spec["workdir"], env, log, shell)
            else:
                proc = subprocess.Popen(argv, cwd=spec["workdir"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, shell=shell, start_new_session=True)
            while True:
                # A process already observed finished is natural completion,
                # not a timeout inferred from a late poll.
                result = proc.poll()
                if result is not None and not tree_active(proc):
                    code = result
                    break
                if interrupted[0] or time.monotonic() - start >= timeout:
                    timed_out = not bool(interrupted[0])
                    stop_tree(proc)
                    code = 124 if timed_out else 128 + interrupted[0]
                    break
                time.sleep(min(0.05, max(0.001, timeout - (time.monotonic() - start))))
    except FileNotFoundError:
        code = 127
        with open(os.path.join(run_dir, "run.log"), "a", encoding="utf-8") as log:
            log.write(traceback.format_exc())
    except Exception:
        if proc is not None:
            stop_tree(proc)
        with open(os.path.join(run_dir, "run.log"), "a", encoding="utf-8") as log:
            log.write(traceback.format_exc())
    if os.name == "nt" and proc is not None:
        proc.close()
    report = {"timeout_seconds": timeout, "timed_out": timed_out,
              "elapsed_seconds": time.monotonic() - start, "exit_code": code}
    write(run_dir, "pilot.json", json.dumps(report, sort_keys=True) + "\n")
    write(run_dir, "exit_code", str(code))
    write(run_dir, "finished_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return code


if __name__ == "__main__":
    sys.exit(main())
'''


def pilot_timeout(metadata: dict[str, Any]) -> int | None:
    """Reject malformed metadata rather than silently running without a limit."""
    value = metadata.get("pilot_seconds")
    if value is not None and (type(value) is not int or not 1 <= value <= 300):
        raise ExecutorError("pilot_seconds must be an integer in 1..300")
    return value


def pilot_files(
    command: PlannedCommand, timeout_seconds: int | None, *, posix_shell: bool = True,
    preamble: list[str] | None = None, calibration_pilot: bool = True,
) -> dict[str, str]:
    """Files to write/upload alongside run.sh; never shell-serialize argv."""
    if timeout_seconds is None:
        return {}
    return {
        "rcf-pilot.py": PILOT_SCRIPT,
        "pilot-args.json": json.dumps({
            "timeout_seconds": timeout_seconds,
            "argv": command.argv,
            "shell": command.shell,
            "workdir": command.workdir,
            "env": command.env,
            "preamble": command.preamble if preamble is None else preamble,
            "posix_shell": posix_shell,
            "calibration_pilot": bool(calibration_pilot),
        }, ensure_ascii=False, sort_keys=True),
    }


_PYTHON_NAME = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.IGNORECASE)


def pilot_interpreter(argv: list[str]) -> str | None:
    """Find a Python token in the planned command, skipping MPI launchers."""
    for argument in argv:
        name = argument.replace("\\", "/").rsplit("/", 1)[-1]
        if _PYTHON_NAME.match(name) and (argument.startswith("/") or "/" not in argument):
            return argument
    return None


def pilot_command(interpreter: str | None = None) -> str:
    """Use the command's resolved interpreter when it is an executable path.

    A bare ``python3`` does not exist on every compute node (notably the lab
    Slurm image), while the planned command already resolved a working path.
    """
    resolved = interpreter or "python3"
    return f'{shquote(resolved)} "$RUN_DIR/rcf-pilot.py" "$RUN_DIR/pilot-args.json"'
