"""Where RCF is allowed to create or modify a Python environment.

The old model was a boolean ("allow environment mutation"). That is the wrong
knob: the risk is not *that* packages are installed, it is *where*. Installing
into ``~/envs/foo`` is harmless; installing into a shared conda ``base`` can
silently change next week's experiment.

The rule here is ownership by location:

* the prefix must live under one of the operator's declared ``managed_roots``;
* a **new** prefix may be created there;
* an **existing** prefix may only be modified when it is declared
  ``managed = true`` or RCF created it itself (recorded in the database);
* certain paths are refused outright, whatever the configuration says.

Everything in this module is a pure function over strings so it can be tested
without a cluster. This is lexical authorization, NOT evidence that the path is
safe on disk: callers must also use path_guard.inspect_prefix_path on the target
node before planning/applying mutations, including for nonexistent prefixes.
Neither check locks a shared filesystem against non-cooperating external writers.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

#: Never touch these, even if someone puts them in ``managed_roots``.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/var",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/public/software",
    "/c/Windows",
    "/c/Program Files",
)


class PathNotAllowed(Exception):
    """Raised with a human-readable reason; the caller turns it into a blocker."""


@dataclass(frozen=True)
class PrefixVerdict:
    allowed: bool
    reason: str

    @property
    def blocked(self) -> bool:
        return not self.allowed


def normalize(path: str) -> str:
    text = str(path).strip().replace("\\", "/")
    if not text:
        raise PathNotAllowed("empty path")
    if ".." in text.split("/"):
        raise PathNotAllowed(f"path contains '..': {path!r}")
    text = posixpath.normpath(text)
    if text != "/":
        text = text.rstrip("/")
    return text


def expand(path: str, home: str | None) -> str:
    """Resolve a leading ``~/`` with the node's home directory."""

    text = str(path).strip().replace("\\", "/")
    if text == "~":
        if not home:
            raise PathNotAllowed("cannot resolve '~' without the node's home directory")
        return normalize(home)
    if text.startswith("~/"):
        if not home:
            raise PathNotAllowed("cannot resolve '~' without the node's home directory")
        return normalize(posixpath.join(home, text[2:]))
    return normalize(text)


def _forbidden(candidate: str) -> str | None:
    for forbidden in FORBIDDEN_PREFIXES:
        if candidate == forbidden or candidate.startswith(forbidden + "/"):
            return forbidden
    return None


def check_prefix(
    *,
    path: str,
    home: str | None,
    managed_roots: list[str],
    exists: bool,
    known_to_rcf: bool,
    declared_managed: bool,
) -> PrefixVerdict:
    """Decide whether RCF may create/modify ``path``.

    ``known_to_rcf`` means the prefix was created by RCF earlier (recorded in
    the database); ``declared_managed`` is the spec-level ``managed = true``.
    This is only the lexical half of authorization. A successful result must be
    followed by target-side ancestor inspection; ``exists`` must not be inferred
    from an unsuccessful filesystem probe.
    """

    try:
        candidate = expand(path, home)
    except PathNotAllowed as exc:
        return PrefixVerdict(False, str(exc))

    forbidden = _forbidden(candidate)
    if forbidden:
        return PrefixVerdict(
            False,
            f"{candidate} is inside the shared/system path {forbidden!r}; "
            "RCF never modifies shared interpreters",
        )

    if not managed_roots:
        return PrefixVerdict(
            False,
            "no managed environment roots are configured; declare "
            "[environments] managed_roots in rcf.toml to let RCF own a directory",
        )

    matched = None
    for root in managed_roots:
        try:
            resolved_root = expand(root, home)
        except PathNotAllowed:
            continue
        if candidate == resolved_root or candidate.startswith(resolved_root + "/"):
            matched = resolved_root
            break
    if matched is None:
        return PrefixVerdict(
            False,
            f"{candidate} is outside the managed roots {managed_roots}",
        )

    if not exists:
        return PrefixVerdict(True, f"will create {candidate} under {matched}")

    if declared_managed:
        return PrefixVerdict(True, f"{candidate} is declared managed by the project config")
    if known_to_rcf:
        return PrefixVerdict(True, f"{candidate} was created by RCF earlier")
    return PrefixVerdict(
        False,
        f"{candidate} already exists but was not created by RCF; if it is yours, "
        "set managed = true for this environment (or move it under a new path)",
    )
