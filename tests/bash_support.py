"""Select a test shell without accidentally invoking Windows' WSL launcher."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from rcf.core.errors import RcfError
from rcf.core.runner import SubprocessRunner

WINDOWS = sys.platform == "win32"


def _candidates() -> list[str]:
    override = os.environ.get("RCF_TEST_BASH")
    if override:
        # An explicit override is authoritative: never silently fall back.
        return [shutil.which(override) or override]
    candidates: list[str] = []
    if WINDOWS:
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git",
        ]
        if local := os.environ.get("LOCALAPPDATA"):
            roots.append(Path(local) / "Programs" / "Git")
        if git := shutil.which("git"):
            root = Path(git).parent.parent
            if root.name.lower() in {"mingw32", "mingw64"}:
                root = root.parent
            roots.append(root)
        for root in roots:
            candidates.extend(str(root / relative) for relative in ("bin/bash.exe", "usr/bin/bash.exe"))
    if on_path := shutil.which("bash"):
        candidates.append(on_path)
    if not WINDOWS:
        candidates.append("/bin/bash")
    return list(dict.fromkeys(candidates))


def find_test_bash() -> str | None:
    """Return a working Bash; Windows tests require Git Bash's MSYS path support."""

    runner = SubprocessRunner()
    for candidate in _candidates():
        path = Path(candidate)
        # Never start the System32/Sysnative WSL compatibility executable, even
        # when it is first on PATH (or supplied as an unsuitable override).
        if WINDOWS and any(part.lower() in {"system32", "sysnative", "windowsapps"} for part in path.parts):
            continue
        if not path.is_file():
            continue
        script = 'test -n "$BASH_VERSION"'
        if WINDOWS:
            script += (
                ' && case "$(uname -s)" in MINGW*|MSYS*) command -v cygpath >/dev/null ;;'
                ' *) exit 1 ;; esac'
            )
        try:
            result = runner.run([str(path), "--noprofile", "--norc", "-c", script], timeout=5)
        except RcfError:
            continue
        if result.ok:
            return str(path)
    return None
