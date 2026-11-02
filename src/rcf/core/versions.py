"""Version comparison helpers (no third-party packaging dependency)."""

from __future__ import annotations

import re

_SPEC_RE = re.compile(r"^(>=|<=|==|!=|~=|>|<)?\s*([0-9][0-9A-Za-z.\-+]*)$")


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value))
    return tuple(int(p) for p in parts) if parts else (0,)


def _pad(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    length = max(len(left), len(right))
    return left + (0,) * (length - len(left)), right + (0,) * (length - len(right))


def compare(left: str, right: str) -> int:
    a, b = _pad(version_tuple(left), version_tuple(right))
    return (a > b) - (a < b)


def satisfies(version: str | None, spec: str | None) -> bool:
    """Check a version against ``>=1.26,<3`` style specs.

    An unparsable spec raises ValueError so a typo in config is visible rather
    than silently treated as "compatible".
    """

    if not spec:
        return True
    if version is None:
        return False
    for raw in spec.split(","):
        clause = raw.strip()
        if not clause:
            continue
        match = _SPEC_RE.match(clause)
        if not match:
            raise ValueError(f"unsupported version spec {clause!r}")
        operator = match.group(1) or "=="
        target = match.group(2)
        if operator == "~=":
            base = version_tuple(target)
            if version_tuple(version)[: max(1, len(base) - 1)] != base[: max(1, len(base) - 1)]:
                return False
            if compare(version, target) < 0:
                return False
            continue
        if operator == "==":
            if target.endswith("*"):
                if not version_tuple(version)[: len(version_tuple(target[:-1]))] == version_tuple(target[:-1]):
                    return False
            elif compare(version, target) != 0:
                return False
        elif operator == "!=":
            if compare(version, target) == 0:
                return False
        elif operator == ">=":
            if compare(version, target) < 0:
                return False
        elif operator == ">":
            if compare(version, target) <= 0:
                return False
        elif operator == "<=":
            if compare(version, target) > 0:
                return False
        elif operator == "<":
            if compare(version, target) >= 0:
                return False
    return True
