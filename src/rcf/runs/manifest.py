"""A deliverable record of what one run was and what it produced.

The database row is enough for RCF to keep working, but it is not something a
reader can hold: it lives in a SQLite file, it changes shape between versions, and
it is not attached to the results.  This module turns one run into a stable JSON
document that can be committed next to a paper, attached to a report, or diffed
against another one.

Everything here is a pure transformation of a payload plus an optional live output
listing; nothing is invented.  When the listing could not be read, the document
says so instead of shipping an empty ``outputs`` list that would look like a run
which produced nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA = "rcf.run.manifest/v1"


def build_manifest(
    payload: dict[str, Any],
    *,
    outputs: dict[str, Any] | None,
    outputs_error: str | None,
    generated_at: str,
) -> dict[str, Any]:
    """Assemble the manifest for one run payload."""

    handle = payload.get("handle") or {}
    command = payload.get("command") or {}
    inputs = payload.get("inputs") or None
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "run": {
            "run_id": payload["run_id"],
            "plan_id": payload.get("plan_id"),
            "project_id": payload.get("project_id"),
            "node_id": payload.get("node_id"),
            "executor": payload.get("executor"),
            "state": payload.get("state"),
            "exit_code": payload.get("exit_code"),
            "failure_kind": payload.get("failure_kind"),
            "sweep_id": payload.get("sweep_id"),
            "created_at": payload.get("created_at"),
            "submitted_at": payload.get("submitted_at"),
            "finished_at": payload.get("finished_at"),
            "duration_seconds": duration_seconds(payload),
            "job_id": handle.get("job_id"),
            # When this record was last confirmed by a probe.  A manifest is a
            # snapshot; without this a reader cannot tell a fresh result from one
            # written before an outage.
            "state_observed_at": payload.get("state_observed_at"),
        },
        "command": {
            "argv": command.get("argv"),
            "workdir": command.get("workdir"),
            "preview": command.get("preview"),
            "shell": command.get("shell"),
            "env": command.get("env") or {},
        },
        "params": payload.get("params") or {},
        "code": {
            "git_commit": payload.get("git_commit"),
            "git_dirty": payload.get("git_dirty"),
        },
        "environment": {
            "env_id": payload.get("env_id"),
            "fingerprint": payload.get("env_fingerprint"),
        },
        "inputs": None
        if inputs is None
        else {
            "digest": inputs.get("digest"),
            "total_bytes": inputs.get("total_bytes"),
            "files": inputs.get("files") or [],
        },
        "outputs": _outputs(payload, outputs, outputs_error),
    }


def _outputs(
    payload: dict[str, Any],
    outputs: dict[str, Any] | None,
    outputs_error: str | None,
) -> dict[str, Any]:
    directory = payload.get("result_dir")
    if outputs_error is not None:
        # Not "empty": unknown.  Saying "no files" here would be a claim about the
        # run that this document cannot support.
        return {"dir": directory, "listed": False, "error": outputs_error, "files": []}
    if outputs is None:
        return {"dir": directory, "listed": False, "error": None, "files": []}
    return {
        "dir": directory,
        "listed": True,
        "error": None,
        "truncated": bool(outputs.get("truncated")),
        "files": [
            {
                "path": entry.get("path"),
                "size": entry.get("size"),
                "sha256": entry.get("sha256"),
            }
            for entry in outputs.get("entries") or []
        ],
    }


def wall_seconds(start: str | None, end: str | None) -> float | None:
    """Wall clock between two stored timestamps, never negative."""

    if not start or not end:
        return None
    try:
        began = datetime.fromisoformat(start)
        stopped = datetime.fromisoformat(end)
    except ValueError:
        return None
    return max(0.0, (stopped - began).total_seconds())


def duration_seconds(payload: dict[str, Any]) -> float | None:
    return wall_seconds(payload.get("submitted_at"), payload.get("finished_at"))
