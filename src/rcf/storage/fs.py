"""One JSON request per short-lived, dependency-free filesystem helper.

Ownership, retained replicas and approval belong to the caller. This layer only
accepts exact fingerprints and enforces the filesystem boundary.
"""
from __future__ import annotations

import base64
import json
import math
import sys
from typing import Any

from ..core.errors import ExecutorError, SpecError
from ..core.models import NodeKind, NodeSpec
from ..core.runner import Runner
from ..executors.ssh import SshTransport
from .fs_script import SCRIPT


def validate_relative(path: str) -> str:
    """Validate a literal POSIX-relative name without stripping whitespace."""
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or (len(path) >= 2 and path[0].isalpha() and path[1] == ":")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise SpecError("storage path must be a literal, normalized relative path", detail={"path": path})
    return path


class StorageFS:
    """Portable bounded inventory and exact-file deletion through Runner only."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def _call(self, node: NodeSpec, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        payload = json.dumps(request, ensure_ascii=True)
        if node.kind is NodeKind.LOCAL:
            result = self.runner.run([sys.executable, "-c", SCRIPT], stdin=payload, timeout=timeout)
        else:
            encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
            # Both values are generated locally; paths never become shell text.
            command = (
                "python3 - <<'RCF_STORAGE_PY'\n"
                + SCRIPT.replace("REQUEST = None", "REQUEST = " + repr(encoded), 1)
                + "\nRCF_STORAGE_PY\n"
            )
            result = SshTransport.from_node(node, self.runner).run(command, timeout=timeout)
        if not result.ok:
            # Never label a timed-out deletion as zero deletions. The caller must
            # reconcile the immutable plan; process termination may lose a reply.
            raise ExecutorError(
                "storage helper failed; mutation outcome may be unknown" if request["op"] == "delete"
                else "storage helper failed",
                detail={"timed_out": result.timed_out, "stderr": result.stderr[:600], "operation": request["op"]},
            )
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ExecutorError("invalid storage helper JSON response") from exc
        if not isinstance(data, dict) or "errors" not in data:
            raise ExecutorError("invalid storage helper response shape")
        if data.get("fencing_error"):
            raise ExecutorError("storage operation " + str(data["fencing_error"]), detail=data)
        return data

    def scan(
        self, node: NodeSpec, root: str, *, max_entries: int = 20000, max_seconds: float = 30,
        excludes: list[str] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(max_entries, int) or max_entries < 0
            or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds)
            or max_seconds < 0 or max_seconds > 3600
        ):
            raise SpecError("storage scan bounds must be non-negative (at most 3600 seconds)")
        names = sorted({str(name) for name in (excludes or []) if name and "/" not in str(name)})
        return self._call(node, {"op": "scan", "root": root, "max_entries": max_entries,
                                 "max_seconds": max_seconds, "exclude_names": names},
                          max_seconds + 10)

    def inspect(self, node: NodeSpec, root: str, paths: list[str], *, hashes: bool = True) -> dict[str, Any]:
        for path in paths:
            validate_relative(path)
        if len(paths) > 20000:
            raise SpecError("storage inspect exceeds 20000 candidates")
        return self._call(node, {"op": "inspect", "root": root, "paths": paths,
                                 "hashes": hashes, "max_seconds": 30}, 40)

    def prepare_delete(self, node: NodeSpec, root: str, plan_id: str) -> dict[str, Any]:
        """Fence quiescent older helpers; a live helper always makes this fail busy."""
        if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 1024:
            raise SpecError("storage operation id must contain 1..1024 characters")
        return self._call(node, {"op": "prepare_delete", "root": root, "operation_id": plan_id,
                                 "max_seconds": 30}, 40)

    def delete(
        self, node: NodeSpec, root: str, entries: list[dict[str, Any]], *,
        operation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(entries) > 20000:
            raise SpecError("storage delete exceeds 20000 candidates")
        if operation is not None:
            if (not isinstance(operation, dict) or not isinstance(operation.get("operation_id"), str)
                    or not operation["operation_id"] or len(operation["operation_id"]) > 1024
                    or type(operation.get("generation")) is not int or operation["generation"] < 1):
                raise SpecError("storage delete requires an operation id and positive integer generation")
        # The helper reports invalid entries individually, preserving partial results.
        return self._call(node, {"op": "delete", "root": root, "entries": entries,
                                 "operation": operation, "max_seconds": 30}, 40)
