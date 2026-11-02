"""A fake ``ssh`` used by the end-to-end tests.

It is *not* a mock of RCF's internals: it accepts the real argv that
:class:`rcf.executors.ssh.SshTransport` produces, then executes the remote
command with a real bash inside a sandbox directory that plays the role of the
remote host.  That exercises the base64 upload, the launch/status/cancel
scripts and the quoting for real.

Environment knobs (set by ``tests/conftest.py``):

* ``RCF_FAKE_SSH_ROOT`` — directory that holds one sub-directory per alias.
* ``RCF_FAKE_SSH_MODE`` — ``hostkey`` | ``auth`` | ``unreachable`` to simulate
  the three failure classes RCF must distinguish.
* ``RCF_TEST_BASH`` — bash binary to use (defaults to the git-bash on PATH).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def to_posix(path: str | Path) -> str:
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def parse_ssh_argv(argv: list[str]) -> tuple[str | None, str]:
    """Extract (alias, remote command) from an OpenSSH-style argv."""

    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            alias = argv[index + 1] if index + 1 < len(argv) else None
            command = argv[index + 2] if index + 2 < len(argv) else ""
            return alias, command
        if token.startswith("-"):
            # Options that take a separate value in the form we emit (-o Key=..).
            if token in {"-o", "-p", "-l", "-i", "-F", "-J", "-L", "-R", "-D", "-W"}:
                index += 2
                continue
            index += 1
            continue
        alias = token
        command = argv[index + 1] if index + 1 < len(argv) else ""
        return alias, command
    return None, ""


def fail(message: str, code: int = 255) -> int:
    sys.stderr.write(message + "\n")
    return code


def main(argv: list[str]) -> int:
    mode = os.environ.get("RCF_FAKE_SSH_MODE", "").strip()
    alias, command = parse_ssh_argv(argv)
    if not alias:
        return fail("ssh: no host specified")
    if mode == "hostkey":
        return fail(
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "Host key verification failed."
        )
    if mode == "auth":
        return fail(f"{alias}: Permission denied (publickey).")
    if mode == "unreachable":
        return fail(f"ssh: connect to host {alias} port 22: Connection refused")

    root = os.environ.get("RCF_FAKE_SSH_ROOT")
    if not root:
        return fail("fake_ssh: RCF_FAKE_SSH_ROOT is not set")
    host = Path(root) / alias
    host.mkdir(parents=True, exist_ok=True)

    bash = os.environ.get("RCF_TEST_BASH") or shutil.which("bash")
    if not bash:
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", "/bin/bash"):
            if Path(candidate).exists():
                bash = candidate
                break
    if not bash:
        return fail("fake_ssh: no bash available")

    env = dict(os.environ)
    env["HOME"] = to_posix(host)
    env["PWD"] = to_posix(host)
    env.pop("RCF_FAKE_SSH_MODE", None)

    # git-bash injects its own /usr/bin ahead of an inherited PATH, so the
    # simulated host exposes its tool directory through BASH_ENV, which bash
    # sources for every non-interactive shell (including `bash -s`).
    fake_bin = to_posix(Path(root) / "bin")
    env["BASH_ENV"] = to_posix(Path(root) / "bin" / "env.sh")

    payload = ""
    if command.strip() in {"bash -s", "bash", "sh -s", "sh"}:
        # Read bytes: a real ssh forwards the byte stream untouched.  Reading
        # text here would silently translate CRLF and hide Windows-side bugs.
        payload = sys.stdin.buffer.read().decode("utf-8", "replace")
        argv = [bash, "-s"]
    else:  # direct command, used by ad-hoc debugging of the fixture
        payload = None
        script = f'export PATH={fake_bin}:"$PATH"' + chr(10) + command
        argv = [bash, "-c", script]

    completed = subprocess.run(
        argv,
        input=payload,
        cwd=str(host),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
