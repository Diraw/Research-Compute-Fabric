"""Fake Slurm CLI (sbatch / squeue / sacct / scancel / sinfo) for e2e tests.

It keeps job state in ``$FAKE_SLURM_DIR`` as JSON files and really executes the
generated batch script, so the RCF status logic is tested against a scheduler
that behaves like a simple one: submit → pending or running → terminal state
with an exit code, plus ``scancel``.

The command is chosen by ``sys.argv[0]`` because the test harness installs one
wrapper script per tool name.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}

SINFO_FIXTURE = """\
gpu*|up|1/3/0/4|8|64000|gpu:a100:4|1-00:00:00|node[001-004]
cpu|up|0/2/0/2|16|128000|(null)|2-00:00:00|node[005-006]
"""


def state_dir() -> Path:
    root = Path(os.environ.get("FAKE_SLURM_DIR", str(Path.home() / ".fake_slurm")))
    (root / "jobs").mkdir(parents=True, exist_ok=True)
    return root


def next_job_id(root: Path) -> str:
    counter = root / "counter"
    for _ in range(50):
        try:
            fd = os.open(str(counter), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(0.01)
            continue
        try:
            current = 1000
            try:
                current = int((root / "counter.value").read_text().strip())
            except (OSError, ValueError):
                pass
            current += 1
            (root / "counter.value").write_text(str(current))
            return str(current)
        finally:
            os.close(fd)
            counter.unlink(missing_ok=True)
    raise SystemExit("fake_slurm: cannot allocate a job id")


def job_path(root: Path, job_id: str) -> Path:
    return root / "jobs" / f"{job_id}.json"


def read_job(root: Path, job_id: str) -> dict | None:
    path = job_path(root, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_job(root: Path, job: dict) -> None:
    path = job_path(root, str(job["id"]))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(path)


def all_jobs(root: Path) -> list[dict]:
    jobs = []
    for path in sorted((root / "jobs").glob("*.json")):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return jobs


def parse_directives(script: Path) -> dict[str, str]:
    directives: dict[str, str] = {}
    for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#SBATCH"):
            continue
        body = stripped[len("#SBATCH") :].strip()
        if not body.startswith("--"):
            continue
        key, _, value = body[2:].partition("=")
        directives[key] = value
    return directives


def cmd_sbatch(argv: list[str]) -> int:
    root = state_dir()
    if os.environ.get("FAKE_SLURM_MODE") == "reject":
        sys.stderr.write("sbatch: error: Batch job submission failed: Invalid partition name\n")
        return 1
    parsable = any(arg == "--parsable" for arg in argv)
    script_arg = next((arg for arg in argv if not arg.startswith("-")), None)
    if not script_arg:
        sys.stderr.write("sbatch: error: no script given\n")
        return 1
    script = Path(script_arg)
    if not script.is_file():
        sys.stderr.write(f"sbatch: error: Unable to open file {script}\n")
        return 1

    directives = parse_directives(script)
    job_id = next_job_id(root)
    job = {
        "id": job_id,
        "name": directives.get("job-name", "rcf-job"),
        "partition": directives.get("partition", "cpu"),
        "gres": directives.get("gres", ""),
        "cpus": directives.get("cpus-per-task", ""),
        "script": str(script.resolve()),
        "cwd": str(Path.cwd()),
        "state": "PENDING",
        "exit_code": None,
        "submitted": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "started": None,
        "finished": None,
        "pid": None,
        "node": "node001",
    }
    write_job(root, job)

    if os.environ.get("FAKE_SLURM_MODE") != "pending":
        job["state"] = "RUNNING"
        job["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_job(root, job)
        _spawn_runner(root, job, script)

    suffix = ";testcluster" if os.environ.get("FAKE_SLURM_PARSABLE_SUFFIX") and parsable else ""
    if parsable:
        sys.stdout.write(f"{job_id}{suffix}\n")
    else:
        sys.stdout.write(f"Submitted batch job {job_id}\n")
    return 0


def _spawn_runner(root: Path, job: dict, script: Path) -> None:
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: the latter pops a visible
        # console window under a ConPTY parent and steals focus during tests.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "__run", str(root), str(job["id"]), str(script)],
        **kwargs,  # type: ignore[arg-type]
    )
    job["pid"] = process.pid
    write_job(root, job)


def cmd_run(argv: list[str]) -> int:
    root = Path(argv[0])
    job_id = argv[1]
    script = Path(argv[2])
    job = read_job(root, job_id) or {"id": job_id}
    started = time.time()
    bash = os.environ.get("RCF_TEST_BASH") or "bash"
    env = dict(os.environ)
    env["SLURM_JOB_ID"] = str(job_id)
    env["SLURM_JOB_NODELIST"] = job.get("node") or "node001"
    completed = subprocess.run(
        [bash, str(script)],
        cwd=job.get("cwd") or str(script.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    job["exit_code"] = completed.returncode
    job["state"] = "COMPLETED" if completed.returncode == 0 else "FAILED"
    job["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    job["elapsed"] = int(time.time() - started)
    job["stdout_tail"] = (completed.stdout or "")[-500:]
    write_job(root, job)
    return 0


def _elapsed(job: dict) -> str:
    if job.get("elapsed") is not None:
        return f"00:00:{int(job['elapsed']):02d}"
    return "00:00:00"


def cmd_squeue(argv: list[str]) -> int:
    root = state_dir()
    job_id = None
    for index, arg in enumerate(argv):
        if arg == "-j" and index + 1 < len(argv):
            job_id = argv[index + 1]
        elif arg.startswith("-j"):
            job_id = arg[2:]
    jobs = all_jobs(root)
    if job_id:
        jobs = [j for j in jobs if str(j.get("id")) == str(job_id)]
    for job in jobs:
        if job.get("state") in TERMINAL:
            continue
        reason = "Priority" if job.get("state") == "PENDING" else "None"
        node = "(Priority)" if job.get("state") == "PENDING" else job.get("node", "node001")
        sys.stdout.write(f"{job['state']}|{_elapsed(job)}|{node}|{reason}\n")
    return 0


def cmd_sacct(argv: list[str]) -> int:
    root = state_dir()
    job_id = None
    for index, arg in enumerate(argv):
        if arg == "-j" and index + 1 < len(argv):
            job_id = argv[index + 1]
        elif arg.startswith("-j"):
            job_id = arg[2:]
    jobs = all_jobs(root)
    if job_id:
        jobs = [j for j in jobs if str(j.get("id")) == str(job_id)]
    for job in jobs:
        if job.get("state") == "PENDING":
            state, exit_code = "PENDING", "0:0"
        elif job.get("state") == "RUNNING":
            state, exit_code = "RUNNING", "0:0"
        elif job.get("state") == "CANCELLED":
            state, exit_code = "CANCELLED", "0:15"
        else:
            code = job.get("exit_code") or 0
            state = "COMPLETED" if code == 0 else "FAILED"
            exit_code = f"{code}:0"
        sys.stdout.write(
            f"{job['id']}|{state}|{exit_code}|{_elapsed(job)}|"
            f"{job.get('started') or 'Unknown'}|{job.get('finished') or 'Unknown'}\n"
        )
        if job.get("state") not in ("PENDING",):
            sys.stdout.write(f"{job['id']}.batch|{state}|{exit_code}|{_elapsed(job)}\n")
    return 0


def cmd_scancel(argv: list[str]) -> int:
    root = state_dir()
    job_ids = [arg for arg in argv if not arg.startswith("-")]
    if not job_ids:
        sys.stderr.write("scancel: error: no job id given\n")
        return 1
    for job_id in job_ids:
        job = read_job(root, job_id)
        if job is None:
            sys.stderr.write(f"scancel: error: Kill job error on job id {job_id}: Invalid job id\n")
            return 1
        pid = job.get("pid")
        if job.get("state") not in TERMINAL and pid:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(int(pid)), 15)
                else:  # pragma: no cover
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        job["state"] = "CANCELLED"
        job["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_job(root, job)
    return 0


def cmd_sinfo(argv: list[str]) -> int:
    sys.stdout.write(SINFO_FIXTURE)
    return 0


def main(argv: list[str]) -> int:
    tool = Path(sys.argv[0]).name
    if tool.endswith(".py"):
        # Invoked as ``python fake_slurm.py <tool> ...`` from a test.
        tool, *argv = argv
    if tool == "sbatch":
        return cmd_sbatch(argv)
    if tool == "squeue":
        return cmd_squeue(argv)
    if tool == "sacct":
        return cmd_sacct(argv)
    if tool == "scancel":
        return cmd_scancel(argv)
    if tool == "sinfo":
        return cmd_sinfo(argv)
    if tool == "__run":
        return cmd_run(argv)
    sys.stderr.write(f"fake_slurm: unknown tool {tool}\n")
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
