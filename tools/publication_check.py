"""Offline, read-only publication hygiene checks over an explicit public allowlist.

This is a heuristic review aid, not proof of absence of secrets. No Git history,
private configuration, environment directories or runtime evidence is traversed.
Matched text is never printed. Use --include-code for the separate fixture/source
review (synthetic paths and addresses in tests may need human classification).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

PUBLIC_FILES = ("README.md", "LICENSE", ".gitignore", "pyproject.toml", "uv.lock")
PUBLIC_DIRS = ("docs", "examples", ".github")
CODE_DIRS = ("src", "tests", "tools")
TEXT_SUFFIXES = {".md", ".toml", ".py", ".yml", ".yaml", ".json", ".txt", ".sh", ".ps1", ".lock"}
SKIP_DIRS = {"__pycache__", ".context", ".venv", ".git", ".pytest_cache", ".ruff_cache"}
# Keep private identifiers out of this checker's own plain-text output/source.
PRIVATE_MARKERS = tuple(left + right for left, right in [
    ("ding", "rui"), ("cra", "ft"), ("gran", "ular"), ("csh", "ape"),
    ("ws-", "kb"), ("ws-", "ubuntu"),
])
PLACEHOLDER_USERS = {"you", "yourname", "researcher", "example", "user", "me", "<you>", "<user>", "shared"}
CREDENTIAL = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
    r"(?:https?|ssh)://[^\s/:]+:[^\s/@]+@)"
)
HOME_PATH = re.compile(
    r"(?:[A-Za-z]:[/\\]Users[/\\]|/Users/|/home/|/public/home/)"
    r"([A-Za-z0-9][A-Za-z0-9._-]*|<[^>]+>)"
)
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F]{0,4}(?![\w:])")
RESERVED_V4 = tuple(ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str


def _unsafe_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _public_paths(root: Path, include_code: bool):
    for name in PUBLIC_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            yield path
    for name in PUBLIC_DIRS + (CODE_DIRS if include_code else ()):
        directory = root / name
        if _unsafe_link(directory):
            yield directory
            continue
        if not directory.is_dir():
            continue
        for current, dirs, files in os.walk(directory, followlinks=False):
            for child in list(dirs):
                path = Path(current) / child
                if _unsafe_link(path):
                    yield path
                    dirs.remove(child)
                elif child in SKIP_DIRS:
                    dirs.remove(child)
            for child in sorted(files):
                path = Path(current) / child
                if path.suffix.lower() in TEXT_SUFFIXES or _unsafe_link(path):
                    yield path


def _non_example_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in net for net in RESERVED_V4)
    return address not in ipaddress.ip_network("2001:db8::/32")


def scan(root: Path, *, include_code: bool = False) -> list[Finding]:
    """Return redacted findings; never follow links or inspect unlisted roots."""
    root = root.resolve()
    findings: list[Finding] = []
    for path in sorted(_public_paths(root, include_code)):
        relative = path.relative_to(root).as_posix()
        if _unsafe_link(path):
            findings.append(Finding(relative, 0, "unsafe-link"))
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            findings.append(Finding(relative, 0, "unreadable-public-file"))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            categories = set()
            if any(marker in line.casefold() for marker in PRIVATE_MARKERS):
                categories.add("private-context")
            if CREDENTIAL.search(line):
                categories.add("credential")
            if any(match.group(1).casefold() not in PLACEHOLDER_USERS for match in HOME_PATH.finditer(line)):
                categories.add("personal-path")
            if any(_non_example_address(match.group()) for pattern in (IPV4, IPV6) for match in pattern.finditer(line)):
                categories.add("network-address")
            findings.extend(Finding(relative, number, category) for category in sorted(categories))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--include-code", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error("source must be an existing directory")
    findings = scan(args.source, include_code=args.include_code)
    if args.json_output:
        print(json.dumps({"ok": not findings, "findings": [asdict(item) for item in findings]}))
    else:
        for item in findings:
            print(f"{item.path}:{item.line}: {item.category}")
        print(f"Publication check: {len(findings)} finding(s); heuristic only, review Git history separately.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
