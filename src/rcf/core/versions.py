"""Version comparison helpers (no third-party packaging dependency)."""

from __future__ import annotations

import re

_SPEC_RE = re.compile(r"^(>=|<=|==|!=|~=|>|<)?\s*([0-9][0-9A-Za-z.\-+]*\*?)$")


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

    A bare version names a series, not a point: ``"3.12"`` means ``3.12.*``,
    so a 3.12.16 interpreter satisfies it.  An explicit operator stays literal:
    ``"==3.12"`` is exactly 3.12, while ``"==3.12.*"`` (and ``"!=3.12.*"``)
    matches the series.  This is deliberate: a compatibility constraint written
    as ``3.12`` should not reject every patch release.

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
        explicit = match.group(1)
        operator = explicit or "=="
        target = match.group(2)
        # A bare value is a series ("3.12" -> 3.12.*); an explicit == is exact.
        wildcard = target.endswith("*") or explicit is None
        if wildcard and operator not in ("==", "!="):
            raise ValueError(f"operator {operator!r} does not accept a wildcard in {clause!r}")
        if operator == "~=":
            base = version_tuple(target)
            if version_tuple(version)[: max(1, len(base) - 1)] != base[: max(1, len(base) - 1)]:
                return False
            if compare(version, target) < 0:
                return False
            continue
        prefix = version_tuple(target[:-1] if target.endswith("*") else target)
        if operator == "==":
            if wildcard:
                if version_tuple(version)[: len(prefix)] != prefix:
                    return False
            elif compare(version, target) != 0:
                return False
        elif operator == "!=":
            if wildcard:
                if version_tuple(version)[: len(prefix)] == prefix:
                    return False
            elif compare(version, target) == 0:
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
