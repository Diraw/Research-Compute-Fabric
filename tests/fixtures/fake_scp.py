"""A fake ``scp`` for the end-to-end tests.

Accepts ``alias:path localpath`` (with any options in front, including ``-O``)
and copies the file out of the sandbox that plays the remote host.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    positionals = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-o", "-P", "-i", "-F", "-J", "-c"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    if len(positionals) < 2:
        sys.stderr.write("fake_scp: usage: scp [opts] host:path local\n")
        return 1
    remote, local = positionals[-2], positionals[-1]
    if ":" not in remote:
        sys.stderr.write("fake_scp: only remote→local copies are supported\n")
        return 1
    alias, _, raw_path = remote.partition(":")
    root = os.environ.get("RCF_FAKE_SSH_ROOT")
    if not root:
        sys.stderr.write("fake_scp: RCF_FAKE_SSH_ROOT is not set\n")
        return 1
    path = raw_path
    if path.startswith("~/"):
        path = str(Path(root) / alias / path[2:])
    # git-bash style "/c/Users/..." -> "C:/Users/..." for the Windows side
    if len(path) > 2 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        path = path[1].upper() + ":" + path[2:]
    source = Path(path)
    if not source.is_file():
        sys.stderr.write(f"fake_scp: {path}: No such file or directory\n")
        return 1
    target = Path(local)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
