"""Compare two runs by what actually differs, not by printing both records.

"Did anything change between these two runs?" is the question a reader has after a
sweep, and the answer is rarely the whole record: usually one parameter moved, or
the code moved and the parameters did not.  This module answers it field by field
and says ``same`` explicitly, so an unchanged field is a finding rather than an
omission.

Nothing here reads a node.  The caller passes payloads and, when it has them, the
output listings; a listing that could not be read becomes ``available: false`` with
the reason, never an empty diff that would read as "same outputs".
"""

from __future__ import annotations

import shlex
from typing import Any

from .manifest import duration_seconds

#: Fields that describe how the run was produced.  Order matters: it is the order
#: a reader checks them in.
PROVENANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("project", "project_id"),
    ("node", "node_id"),
    ("executor", "executor"),
    ("commit", "git_commit"),
    ("dirty", "git_dirty"),
    ("env", "env_id"),
    ("env fingerprint", "env_fingerprint"),
)

OUTCOME_FIELDS: tuple[tuple[str, str], ...] = (
    ("state", "state"),
    ("exit", "exit_code"),
    ("verdict", "failure_kind"),
    ("sweep", "sweep_id"),
)


def compare_runs(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    outputs_left: dict[str, Any] | None = None,
    outputs_right: dict[str, Any] | None = None,
    outputs_reason: str | None = None,
) -> dict[str, Any]:
    """Build the comparison document for two run payloads."""

    params = _compare_params(left.get("params") or {}, right.get("params") or {})
    provenance = _compare_fields(left, right, PROVENANCE_FIELDS)
    provenance.append(_compare_inputs(left, right))
    outcome = _compare_fields(left, right, OUTCOME_FIELDS)
    outcome.append(
        {
            "name": "duration",
            "left": _seconds(duration_seconds(left)),
            "right": _seconds(duration_seconds(right)),
            "same": _rounded(duration_seconds(left)) == _rounded(duration_seconds(right)),
        }
    )
    return {
        "left": left.get("run_id"),
        "right": right.get("run_id"),
        "params": params,
        "provenance": provenance,
        "outcome": outcome,
        "command": {
            "same": _invocation(left) == _invocation(right),
            "left": _invocation(left),
            "right": _invocation(right),
        },
        "outputs": _compare_outputs(
            left, right, outputs_left, outputs_right, outputs_reason
        ),
        "changed": any(
            item["status"] != "same" for item in params
        )
        or any(not item["same"] for item in provenance)
        or any(not item["same"] for item in outcome)
        or _invocation(left) != _invocation(right),
    }


def _compare_params(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(set(left) | set(right)):
        if key not in left:
            status = "only right"
        elif key not in right:
            status = "only left"
        elif left[key] == right[key]:
            status = "same"
        else:
            status = "changed"
        rows.append(
            {"key": key, "left": left.get(key), "right": right.get(key), "status": status}
        )
    return rows


def _compare_fields(
    left: dict[str, Any], right: dict[str, Any], fields: tuple[tuple[str, str], ...]
) -> list[dict[str, Any]]:
    return [
        {"name": label, "left": left.get(key), "right": right.get(key), "same": left.get(key) == right.get(key)}
        for label, key in fields
    ]


def _compare_inputs(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    lhs = (left.get("inputs") or {}).get("digest")
    rhs = (right.get("inputs") or {}).get("digest")
    return {
        "name": "inputs",
        "left": (lhs or "")[:12] or None,
        "right": (rhs or "")[:12] or None,
        "same": lhs == rhs,
    }


def _compare_outputs(
    left: dict[str, Any],
    right: dict[str, Any],
    outputs_left: dict[str, Any] | None,
    outputs_right: dict[str, Any] | None,
    reason: str | None,
) -> dict[str, Any]:
    if reason is not None or outputs_left is None or outputs_right is None:
        return {
            "available": False,
            "reason": reason or "outputs were not listed",
            "only_left": [],
            "only_right": [],
            "changed": [],
            "same": 0,
        }

    lhs = {entry.get("path"): entry for entry in outputs_left.get("entries") or []}
    rhs = {entry.get("path"): entry for entry in outputs_right.get("entries") or []}
    changed = []
    for path in sorted(set(lhs) & set(rhs)):
        before, after = lhs[path], rhs[path]
        if before.get("size") == after.get("size") and _same_hash(before, after):
            continue
        changed.append(
            {
                "path": path,
                "left_size": before.get("size"),
                "right_size": after.get("size"),
                "left_sha256": before.get("sha256"),
                "right_sha256": after.get("sha256"),
            }
        )
    same = len(set(lhs) & set(rhs)) - len(changed)
    return {
        "available": True,
        "reason": None,
        "only_left": sorted(set(lhs) - set(rhs)),
        "only_right": sorted(set(rhs) - set(lhs)),
        "changed": changed,
        "same": same,
    }


def _same_hash(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Two hashes count as equal only when both are present and match.

    A size that happens to match is not evidence of identical content, and a missing
    hash is not evidence of anything.
    """

    return bool(left.get("sha256")) and left.get("sha256") == right.get("sha256")


def _invocation(payload: dict[str, Any]) -> str:
    """The command itself, without the per-run environment.

    The stored preview includes ``RCF_OUTPUT_DIR`` and the run id, so comparing
    previews would report every pair of runs as "different command" no matter what
    was executed.  argv is the part that answers "what did this run do".
    """

    command = payload.get("command") or {}
    argv = [str(token) for token in command.get("argv") or []]
    if command.get("shell"):
        return argv[0] if argv else ""
    return " ".join(shlex.quote(token) for token in argv)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
