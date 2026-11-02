"""Generated shell scripts for remote (SSH) and scheduler (Slurm) execution.

The scripts are the contract for crash-safe job tracking: they are plain files
in the run directory, so the state of a job survives the SSH connection, the RCF
process and even a reboot of the control machine.

Directory layout for every run::

    <work_root>/runs/<run_id>/
        run.json        metadata (project, params, env, command)
        run.sh          the generated script
        pid             pid of the running script
        pid_start       kernel start tick of that process instance
        pid_boot        kernel boot id (when /proc exposes it)
        started_at      ISO-8601 UTC
        finished_at     ISO-8601 UTC
        exit_code       written atomically when the command finishes
        run.log         combined stdout/stderr of the command
        env.log         output of environment setup (module load, conda ...)
"""

from __future__ import annotations

import posixpath

from ..core.models import PlannedCommand
from ..core.runner import shquote
from ..progress.pilot import pilot_command

STATUS_PROBE = r"""
cd {run_dir} 2>/dev/null || {{ echo state=MISSING; exit 0; }}
pid=$(cat pid 2>/dev/null || true)
if [ -f exit_code ]; then
  echo state=EXITED
  echo exit_code=$(cat exit_code 2>/dev/null)
elif [ -z "$pid" ]; then
  echo state=STARTING
else
  if kill -0 "$pid" 2>/dev/null; then
    echo state=RUNNING
    if [ -r "/proc/$pid/cmdline" ]; then
      if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "run.sh"; then
        echo pid_marker=ok
      else
        echo pid_marker=mismatch
      fi
    else
      echo pid_marker=unknown
    fi
  else
    echo state=LOST
  fi
fi
echo pid=${{pid}}
echo started_at=$(cat started_at 2>/dev/null || true)
echo finished_at=$(cat finished_at 2>/dev/null || true)
echo log_bytes=$(wc -c < run.log 2>/dev/null || echo 0)
"""


def run_script(
    *,
    run_id: str,
    run_dir: str,
    command: PlannedCommand,
    workdir: str,
    preamble: list[str] | None = None,
    created_at: str = "",
    pilot_seconds: int | None = None,
    pilot_interpreter: str | None = None,
) -> str:
    """Generate the POSIX script that actually executes the user command."""

    header = [
        "#!/usr/bin/env bash",
        f"# RCF run {run_id}" + (f" generated {created_at}" if created_at else ""),
        "# This file is managed by RCF; edit the project config instead.",
        "set -o pipefail",
        f"RUN_DIR={shquote(run_dir)}",
        f"cd {shquote(workdir)} || {{ "
        f"printf '%s\\n' 97 > \"$RUN_DIR/.exit_code.tmp\"; "
        f"mv \"$RUN_DIR/.exit_code.tmp\" \"$RUN_DIR/exit_code\"; exit 97; }}",
        'printf \'%s\\n\' $$ > "$RUN_DIR/pid"',
        'date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/started_at"',
        # A PID alone is reusable; unreadable /proc makes cancel fail closed.
        "sed -E 's/.*[)] //' /proc/$$/stat 2>/dev/null | awk '{print $20}' > \"$RUN_DIR/pid_start\"",
        'cat /proc/sys/kernel/random/boot_id 2>/dev/null > "$RUN_DIR/pid_boot" || true',
    ]
    env_lines = [f"export {key}={shquote(value)}" for key, value in sorted(command.env.items())]
    setup = [] if pilot_seconds is not None else list(
        preamble if preamble is not None else command.preamble
    )
    if pilot_seconds is not None:
        env_lines = []
    body_command = (
        " ".join(shquote(a) for a in command.argv)
        if not command.shell
        else " ".join(command.argv)
    )
    lines = header
    if setup:
        lines.append("{")
        lines.extend(f"  {line}" for line in setup)
        lines.append('} > "$RUN_DIR/env.log" 2>&1')
    else:
        lines.append('printf "%s\\n" "no environment setup" > "$RUN_DIR/env.log"')
    lines.extend(env_lines)
    lines.extend(
        [
            '{ printf "# command\\n%s\\n# workdir\\n%s\\n" '
            + shquote(body_command)
            + " "
            + shquote(workdir)
            + ' > "$RUN_DIR/run.log"; }',
            "(\n" + (pilot_command(pilot_interpreter) if pilot_seconds is not None else body_command)
            + '\n) >> "$RUN_DIR/run.log" 2>&1',
            "rc=$?",
            'printf \'%s\\n\' "$rc" > "$RUN_DIR/.exit_code.tmp"',
            'mv "$RUN_DIR/.exit_code.tmp" "$RUN_DIR/exit_code"',
            'date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/finished_at"',
            "exit $rc",
        ]
    )
    return "\n".join(lines) + "\n"


def bootstrap_command(run_dir: str) -> str:
    """Create/attach to the detached script and return immediately.

    ``setsid`` gives the run its own session so that cancelling kills the whole
    tree and so that closing the SSH channel cannot take the job down.  When
    ``setsid`` is unavailable the script falls back to ``nohup`` and cancel
    degrades to per-pid signalling (documented limitation).
    """

    q = shquote(run_dir)
    return (
        f"cd {q} || exit 96; chmod +x run.sh 2>/dev/null || true; "
        "if command -v setsid >/dev/null 2>&1; then "
        "setsid bash run.sh > bootstrap.log 2>&1 < /dev/null & "
        "else nohup bash run.sh > bootstrap.log 2>&1 < /dev/null & fi; "
        "echo rcf_bootstrap=ok"
    )


def cancel_command(run_dir: str) -> str:
    q = shquote(run_dir)
    return f"cd {q} 2>/dev/null || {{ echo state=MISSING; exit 0; }};\n" + r'''
pid=$(cat pid 2>/dev/null || true)
case "$pid" in ''|*[!0-9]*|0|1) echo cancelled=no_pid; echo state=UNKNOWN; exit 0 ;; esac
if [ -f exit_code ]; then echo cancelled=already_exited; echo state=EXITED; exit 0; fi
expected=$(cat pid_start 2>/dev/null || true)
run_script="$(pwd -P)/run.sh"
identity() {
  [ -n "$expected" ] || return 1
  [ "$(cat pid 2>/dev/null)" = "$pid" ] || return 1
  [ "$(cat pid_start 2>/dev/null)" = "$expected" ] || return 1
  current=$(sed -E 's/.*[)] //' "/proc/$pid/stat" 2>/dev/null | awk '{print $20}')
  [ -n "$current" ] && [ "$current" = "$expected" ] || return 1
  # Bash keeps its script open on fd 255 even when invoked as `bash run.sh`.
  # A generic run.sh cmdline substring cannot bind a PID to this run directory.
  actual=$(readlink "/proc/$pid/fd/255" 2>/dev/null || true)
  [ "$actual" = "$run_script" ] || return 1
  boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
  saved_boot=$(cat pid_boot 2>/dev/null || true)
  [ "$boot" = "$saved_boot" ] || return 1
}
if ! identity; then echo cancelled=identity_mismatch; echo state=UNKNOWN; exit 0; fi
pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
# Revalidate immediately before EACH destructive signal (including escalation).
if ! identity; then echo cancelled=identity_changed; echo state=UNKNOWN; exit 0; fi
if [ "$pgid" = "$pid" ]; then kill -TERM -"$pid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
if [ $? -ne 0 ]; then echo cancelled=signal_failed; echo state=UNKNOWN; exit 0; fi
i=0
while kill -0 "$pid" 2>/dev/null && [ $i -lt 10 ]; do sleep 1; i=$((i+1)); done
if kill -0 "$pid" 2>/dev/null; then
  if ! identity; then echo cancelled=identity_changed; echo state=UNKNOWN; exit 0; fi
  if [ "$pgid" = "$pid" ]; then kill -KILL -"$pid" 2>/dev/null; else kill -KILL "$pid" 2>/dev/null; fi
  if [ $? -ne 0 ]; then echo cancelled=signal_failed; echo state=UNKNOWN; exit 0; fi
  i=0
  while kill -0 "$pid" 2>/dev/null && [ $i -lt 3 ]; do sleep 1; i=$((i+1)); done
  if kill -0 "$pid" 2>/dev/null; then echo cancelled=still_alive; echo state=UNKNOWN; exit 0; fi
  echo cancelled=force
else
  echo cancelled=term
fi
echo state=CANCELLED
'''


def tail_command(run_dir: str, tail: int) -> str:
    q = shquote(posixpath.join(run_dir, "run.log"))
    return f"if [ -f {q} ]; then tail -n {int(tail)} -- {q}; else echo 'RCF: run.log not found'; fi"


def newer_files_command(*, root: str, reference: str, limit: int = 50) -> str:
    """Portable JSON output probe, including filenames containing newlines."""
    from ..storage.output_probe import output_command

    return output_command(root, limit=limit, reference=reference)


def manifest_command(result_dir: str, limit: int = 200, *, hashes: bool = True) -> str:
    """Complete bounded output listing; overflow is an error."""
    from ..storage.output_probe import output_command

    return output_command(result_dir, limit=limit, hashes=hashes)


def git_state_command(project_path: str) -> str:
    q = shquote(project_path)
    return (
        f"cd {q} 2>/dev/null || {{ echo rcf_repo=missing; exit 0; }}; "
        'if [ -d .git ]; then echo rcf_repo=git; '
        "echo rcf_commit=$(git rev-parse HEAD 2>/dev/null); "
        "echo rcf_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null); "
        "if [ -n \"$(git status --porcelain 2>/dev/null)\" ]; then echo rcf_dirty=yes; "
        "else echo rcf_dirty=no; fi; else echo rcf_repo=no_git; fi"
    )


def mkdir_command(path: str) -> str:
    return f"mkdir -p {shquote(path)}"
