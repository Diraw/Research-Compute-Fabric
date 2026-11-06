"""JSON-only storage governance ledger sharing the run Store's SQLite connection.

No method inspects files or contacts nodes. Guards have no TTL: an uncertain
mutation must retain its exclusion until the caller explicitly reconciles it.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..core.errors import Conflict, NotFound, SpecError, StoreError
from ..core.ids import iso, new_id, now_utc, stable_hash
from ..runs.store import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_meta (
    key TEXT PRIMARY KEY, value INTEGER NOT NULL
);
INSERT OR IGNORE INTO storage_meta(key, value) VALUES ('revision', 0);
CREATE TABLE IF NOT EXISTS storage_snapshots (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, node_id TEXT NOT NULL, root_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS storage_ix_snapshots_scope
    ON storage_snapshots(project_id, node_id, root_id, seq DESC);
CREATE INDEX IF NOT EXISTS storage_ix_snapshots_node ON storage_snapshots(node_id, seq DESC);
CREATE TABLE IF NOT EXISTS storage_owned (
    project_id TEXT NOT NULL, node_id TEXT NOT NULL, root TEXT NOT NULL,
    path TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (node_id, root, path)
);
CREATE INDEX IF NOT EXISTS storage_ix_owned_scope ON storage_owned(project_id, node_id, root);
CREATE TABLE IF NOT EXISTS storage_pins (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (project_id, run_id)
);
CREATE TABLE IF NOT EXISTS storage_plans (
    plan_id TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_copies (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE, project_id TEXT, run_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS storage_ix_copies_scope ON storage_copies(project_id, run_id, seq);
CREATE INDEX IF NOT EXISTS storage_ix_copies_run ON storage_copies(run_id, seq);
CREATE TABLE IF NOT EXISTS storage_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL, path TEXT NOT NULL, payload TEXT NOT NULL,
    UNIQUE (plan_id, path)
);
CREATE TABLE IF NOT EXISTS storage_guard_nodes (
    node_id TEXT PRIMARY KEY, physical_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_guards (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    guard_id TEXT NOT NULL UNIQUE, node_id TEXT NOT NULL,
    released_at TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS storage_ix_guards_active
    ON storage_guards(node_id, seq) WHERE released_at IS NULL;
"""


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    raise SpecError(f"storage ledger value is not JSON serializable: {type(value).__name__}")


def _dump(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"invalid storage ledger JSON: {exc}") from exc


def _copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_dump(value))


def physical_identities(nodes: Mapping[str, Any]) -> dict[str, str]:
    """Connection-endpoint identities shared by guards, plans and fetch checks.

    Only routing fields identify the command endpoint: display names, work
    roots, executor kind and SSH timeouts do not turn overlapping paths into
    independent filesystems.
    """

    identities = {
        name: ("local:controller" if node.kind.value == "local" else "ssh:" + stable_hash({
            "alias": node.ssh_alias,
            "program": node.ssh.program,
            "options": node.ssh.extra_options,
        }, length=64))
        for name, node in nodes.items()
    }
    identities["controller"] = "local:controller"
    return identities


def _root_key(root: str) -> tuple[str, ...]:
    """Lexical components only; never resolve symlinks or touch a filesystem."""
    if not isinstance(root, str) or not root or "\x00" in root:
        raise SpecError("guard roots must be nonempty path strings")
    windows = PureWindowsPath(root)
    if (len(root) > 1 and root[1] == ":") or root.startswith("\\\\"):
        if not windows.is_absolute():
            raise SpecError("guard Windows roots must be absolute", detail={"root": root})
        return ("windows", *(part.casefold() for part in PureWindowsPath(ntpath.normpath(root)).parts))
    normalized = PurePosixPath(
        posixpath.normpath("/" + root.lstrip("/")) if root.startswith("/") else posixpath.normpath(root)
    )
    if not normalized.is_absolute():
        raise SpecError("guard roots must be absolute", detail={"root": root})
    return ("posix", *normalized.parts)


def _overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    length = min(len(left), len(right))
    return left[:length] == right[:length]


class Ledger:
    """Persistence adapter; all returned dictionaries are detached JSON values.

    List queries are indexed and fetched in bounded SQLite batches. Their public
    contract returns all matching records (never silently truncates history).
    """

    def __init__(self, store: Store, *, nodes=None) -> None:
        self._conn = store._conn
        self._lock = store._lock
        with self._transaction():
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self._conn.execute(statement)
            if nodes is not None:
                # Persist identities for every adapter sharing this database,
                # including fresh connections used by submit/push/fetch.
                identities = physical_identities(nodes)
                # This is a current-config index, not ownership evidence. Keep
                # removed nodes absent so their persisted guards fail closed.
                self._conn.execute("DELETE FROM storage_guard_nodes")
                self._conn.executemany(
                    "INSERT INTO storage_guard_nodes(node_id,physical_id) VALUES (?,?)"
                    " ON CONFLICT(node_id) DO UPDATE SET physical_id=excluded.physical_id",
                    identities.items(),
                )

    def physical_node(self, node_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT physical_id FROM storage_guard_nodes WHERE node_id=?", (node_id,),
            ).fetchone()
            return row[0] if row else ("local:controller" if node_id == "controller" else "unconfigured:" + node_id)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # The shared RLock covers BEGIN through COMMIT, not individual statements.
        # Refuse nesting rather than committing or rolling back a caller's work.
        with self._lock:
            if self._conn.in_transaction:
                raise Conflict("storage ledger cannot enter an existing Store transaction")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"storage ledger transaction failed: {exc}") from exc
            except BaseException:
                self._conn.rollback()
                raise

    def _rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                cursor = self._conn.execute(query, params)
                result = []
                while batch := cursor.fetchmany(256):
                    result.extend(json.loads(row["payload"]) for row in batch)
                return result
            except sqlite3.Error as exc:
                raise StoreError(f"storage ledger read failed: {exc}") from exc

    @staticmethod
    def _where(**filters: Any) -> tuple[str, tuple[Any, ...]]:
        # Column names are internal constants, never caller-supplied SQL.
        active = {key: value for key, value in filters.items() if value is not None}
        clause = " AND ".join(f"{key} = ?" for key in active)
        return (f" WHERE {clause}" if clause else "", tuple(active.values()))

    def _revision(self) -> int:
        return int(self._conn.execute("SELECT value FROM storage_meta WHERE key = 'revision'").fetchone()[0])

    def _bump(self) -> None:
        self._conn.execute("UPDATE storage_meta SET value = value + 1 WHERE key = 'revision'")

    def revision(self) -> int:
        with self._lock:
            try:
                return self._revision()
            except sqlite3.Error as exc:
                raise StoreError(f"cannot read storage protection revision: {exc}") from exc

    def save_snapshot(self, snapshot: dict[str, Any]) -> str:
        data = _copy(snapshot)
        scan_id = data.get("scan_id") or new_id("scan")
        data["scan_id"] = scan_id
        data.setdefault("created_at", iso(now_utc()))
        with self._transaction():
            self._conn.execute(
                "INSERT INTO storage_snapshots(scan_id, project_id, node_id, root_id, payload)"
                " VALUES (?,?,?,?,?) ON CONFLICT(scan_id) DO UPDATE SET"
                " project_id=excluded.project_id, node_id=excluded.node_id,"
                " root_id=excluded.root_id, payload=excluded.payload",
                (scan_id, data["project_id"], data["node_id"], data["root_id"], _dump(data)),
            )
        return scan_id

    def latest_snapshot(self, project_id: str, node_id: str, root_id: str) -> dict[str, Any] | None:
        records = self._rows(
            "SELECT payload FROM storage_snapshots WHERE project_id=? AND node_id=? AND root_id=?"
            " ORDER BY seq DESC LIMIT 1", (project_id, node_id, root_id),
        )
        return records[0] if records else None

    def snapshots(self, project_id: str | None = None, node_id: str | None = None) -> list[dict[str, Any]]:
        clause, params = self._where(project_id=project_id, node_id=node_id)
        return self._rows("SELECT payload FROM storage_snapshots" + clause + " ORDER BY seq DESC", params)

    def record_owned(
        self, project_id: str, node_id: str, root: str, entries: list[dict[str, Any]], *,
        kind: str, owner_id: str, source_node: str | None = None, source_root: str | None = None,
    ) -> None:
        data_entries = [_copy(entry) for entry in entries]
        with self._transaction():
            changed = False
            for entry in data_entries:
                row = self._conn.execute(
                    "SELECT payload FROM storage_owned WHERE node_id=? AND root=? AND path=?",
                    (node_id, root, entry["path"]),
                ).fetchone()
                previous = json.loads(row["payload"]) if row else {}
                data = {**entry, "project_id": project_id, "node_id": node_id, "root": root,
                        "kind": kind, "owner_id": owner_id,
                        "policy": entry.get("policy", previous.get("policy", "protected")),
                        "source_node": source_node, "source_root": source_root}
                if data == {key: value for key, value in previous.items() if key != "updated_at"}:
                    continue
                data["updated_at"] = iso(now_utc())
                self._conn.execute(
                    "INSERT INTO storage_owned(project_id, node_id, root, path, payload) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(node_id, root, path) DO UPDATE SET"
                    " project_id=excluded.project_id, payload=excluded.payload",
                    (project_id, node_id, root, data["path"], _dump(data)),
                )
                changed = True
            if changed:
                self._bump()

    def owned(
        self, project_id: str | None = None, node_id: str | None = None, root: str | None = None,
    ) -> list[dict[str, Any]]:
        clause, params = self._where(project_id=project_id, node_id=node_id, root=root)
        return self._rows("SELECT payload FROM storage_owned" + clause + " ORDER BY node_id, root, path", params)

    def set_pin(self, project_id: str, run_id: str, pinned: bool, reason: str) -> dict[str, Any]:
        with self._transaction():
            data = {"project_id": project_id, "run_id": run_id, "pinned": bool(pinned), "reason": reason}
            row = self._conn.execute(
                "SELECT payload FROM storage_pins WHERE project_id=? AND run_id=?", (project_id, run_id),
            ).fetchone()
            previous = json.loads(row["payload"]) if row else None
            if previous and all(previous.get(key) == value for key, value in data.items()):
                return previous
            data["updated_at"] = iso(now_utc())
            self._conn.execute(
                "INSERT INTO storage_pins(project_id, run_id, payload) VALUES (?,?,?)"
                " ON CONFLICT(project_id, run_id) DO UPDATE SET payload=excluded.payload",
                (project_id, run_id, _dump(data)),
            )
            self._bump()
            return data

    def pins(self, project_id: str | None = None) -> list[dict[str, Any]]:
        clause, params = self._where(project_id=project_id)
        return self._rows("SELECT payload FROM storage_pins" + clause + " ORDER BY project_id, run_id", params)

    def save_plan(self, plan: dict[str, Any]) -> str:
        data = _copy(plan)
        plan_id = data.get("plan_id") or new_id("storageplan")
        data["plan_id"] = plan_id
        data.setdefault("created_at", iso(now_utc()))
        data.setdefault("status", "planned")
        with self._transaction():
            data.setdefault("revision", self._revision())
            if self._conn.execute("SELECT 1 FROM storage_plans WHERE plan_id=?", (plan_id,)).fetchone():
                raise Conflict("storage plan selection is immutable; create a new plan id")
            self._conn.execute(
                "INSERT INTO storage_plans(plan_id, payload) VALUES (?,?)", (plan_id, _dump(data)),
            )
        return plan_id

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        records = self._rows("SELECT payload FROM storage_plans WHERE plan_id=?", (plan_id,))
        return records[0] if records else None

    def update_plan(
        self, plan_id: str, status: str, report: dict[str, Any] | None = None, *,
        owner_token: str | None = None, stage: str | None = None,
        target_operation: dict[str, Any] | None = None,
    ) -> None:
        with self._transaction():
            row = self._conn.execute("SELECT payload FROM storage_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise NotFound("unknown storage plan", detail={"plan_id": plan_id})
            data = json.loads(row["payload"])
            execution = data.get("execution") or {}
            if execution.get("token") and owner_token != execution["token"]:
                raise Conflict("stale storage execution token; refusing state overwrite")
            if data.get("status") in {"completed", "superseded"}:
                raise Conflict("completed storage plan is immutable")
            data.update(status=status, updated_at=iso(now_utc()))
            if execution:
                execution.update(heartbeat_at=iso(now_utc()), stage=stage or status)
                if target_operation is not None:
                    execution["target_operation"] = _copy(target_operation)
                data["execution"] = execution
            if report is not None:
                data["report"] = _copy(report)
            self._conn.execute("UPDATE storage_plans SET payload=? WHERE plan_id=?", (_dump(data), plan_id))

    def record_copy(self, receipt: dict[str, Any]) -> None:
        data = _copy(receipt)
        receipt_id = data.get("receipt_id") or data.get("copy_id") or new_id("copy")
        data["receipt_id"] = receipt_id
        data.setdefault("created_at", iso(now_utc()))
        with self._transaction():
            self._conn.execute(
                "INSERT INTO storage_copies(receipt_id, project_id, run_id, payload) VALUES (?,?,?,?)"
                " ON CONFLICT(receipt_id) DO UPDATE SET project_id=excluded.project_id,"
                " run_id=excluded.run_id, payload=excluded.payload",
                (receipt_id, data.get("project_id"), data.get("run_id"), _dump(data)),
            )
            self._bump()

    def copies(self, project_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        clause, params = self._where(project_id=project_id, run_id=run_id)
        return self._rows("SELECT payload FROM storage_copies" + clause + " ORDER BY seq", params)

    def journal(self, plan_id: str, path: str, status: str, detail: dict[str, Any] | None = None) -> None:
        with self._transaction():
            row = self._conn.execute(
                "SELECT payload FROM storage_journal WHERE plan_id=? AND path=?", (plan_id, path),
            ).fetchone()
            data = {"plan_id": plan_id, "path": path, "status": status, "detail": detail}
            if row:
                previous = json.loads(row["payload"])
                if all(previous.get(key) == value for key, value in _copy(data).items()):
                    return
            data["updated_at"] = iso(now_utc())
            self._conn.execute(
                "INSERT INTO storage_journal(plan_id, path, payload) VALUES (?,?,?)"
                " ON CONFLICT(plan_id, path) DO UPDATE SET payload=excluded.payload",
                (plan_id, path, _dump(data)),
            )

    def journal_entries(self, plan_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT payload FROM storage_journal WHERE plan_id=? ORDER BY seq", (plan_id,))

    def acquire_guards(
        self, node_id: str, roots: list[str], *, operation: str, owner_id: str,
        resume_owner: str | None = None,
    ) -> list[str]:
        """Claim all roots or none.

        ``resume_owner`` reuses guards left by an interrupted exact-plan apply;
        it can only adopt guards with the same owner and operation. It never
        steals another operation's lock or guesses from elapsed time.
        """
        if not roots or isinstance(roots, (str, bytes)):
            raise SpecError("at least one guard root is required")
        roots = list(dict.fromkeys(roots))
        keys = [_root_key(root) for root in roots]
        with self._transaction():
            physical = self.physical_node(node_id)
            identities = dict(self._conn.execute("SELECT node_id, physical_id FROM storage_guard_nodes"))
            active = []
            for row in self._conn.execute("SELECT payload FROM storage_guards WHERE released_at IS NULL"):
                old = json.loads(row["payload"])
                old_physical = old.get("physical_id")
                # A legacy guard's logical name cannot prove its old endpoint:
                # the alias may have disappeared or changed local/remote kind.
                # Keep overlapping legacy scopes excluded on every host until
                # exact-plan recovery; never rewrite or release uncertain work.
                legacy = (old_physical is None or old_physical.startswith("node:")
                          or (bool(identities) and old_physical.startswith("unconfigured:")))
                # Persisted endpoint evidence must not be reinterpreted through
                # today's config. Removed/repointed endpoints remain uncertain
                # on every host, including aliases newly pointing at that node.
                drifted = (old_physical is not None and old_physical.startswith(("ssh:", "local:"))
                           and identities.get(old["node_id"]) != old_physical)
                if legacy or drifted or old_physical == physical or old["node_id"] == node_id:
                    active.append(row)
            adopted: list[str] = []
            remaining: list[str] = []
            for root, key in zip(roots, keys, strict=False):
                matches = [json.loads(row["payload"]) for row in active
                           if any(_overlap(key, _root_key(old))
                                  for old in json.loads(row["payload"])["roots"])]
                if matches:
                    for found in matches:
                        if not (resume_owner and found.get("owner_id") == resume_owner
                                and found.get("operation") == operation):
                            raise Conflict("storage roots already guarded", detail={"guard": found})
                        adopted.append(found["guard_id"])
                else:
                    remaining.append(root)
            tokens = list(dict.fromkeys(adopted))
            if remaining:
                guard_id = new_id("guard")
                data = {"guard_id": guard_id, "node_id": node_id, "physical_id": physical, "roots": remaining,
                        "operation": operation, "owner_id": owner_id, "created_at": iso(now_utc()),
                        "released_at": None, "status": "active"}
                self._conn.execute(
                    "INSERT INTO storage_guards(guard_id, node_id, payload) VALUES (?,?,?)",
                    (guard_id, node_id, _dump(data)),
                )
                tokens.append(guard_id)
            return tokens

    def acquire_guard(self, node_id: str, roots: list[str], *, operation: str, owner_id: str) -> str:
        return self.acquire_guards(node_id, roots, operation=operation, owner_id=owner_id)[0]

    def claim_plan(
        self, plan_id: str, *, allow_resume: bool = False, owner: dict[str, Any] | None = None,
        previous_token: str | None = None, ownership_verified: bool = False,
    ) -> dict[str, Any]:
        """Atomically make one process responsible for executing a plan.

        A terminal plan is returned read-only. An interrupted cleanup (unknown)
        or a partial run may only continue with an explicit resume request, and
        only against the exact stored file fingerprints; it never reselects
        candidates. Two simultaneous applies cannot both win.
        """

        with self._transaction():
            row = self._conn.execute(
                "SELECT payload FROM storage_plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if row is None:
                raise NotFound("unknown storage plan", detail={"plan_id": plan_id})
            data = json.loads(row["payload"])
            status = data.get("status", "planned")
            previous = status
            if status in {"completed", "superseded"}:
                return {**data, "_claimed": False, "_previous_status": previous}
            prior_owner = data.get("execution") or {}
            if previous_token != prior_owner.get("token"):
                raise Conflict("storage execution owner changed during recovery; inspect again")
            if status in {"applying", "unknown"}:
                if not allow_resume or not ownership_verified or owner is None:
                    raise Conflict(
                        "storage plan needs explicit resume and verified controller execution ownership",
                        detail={"plan_id": plan_id, "status": status, "execution": prior_owner},
                    )
                if prior_owner and (prior_owner.get("controller") != owner.get("controller")
                                    or prior_owner.get("lock_scope") != owner.get("lock_scope")):
                    raise Conflict("foreign/unverifiable controller lock scope; automatic takeover refused")
                if not prior_owner and status == "applying":
                    active = self._conn.execute("SELECT payload FROM storage_guards WHERE released_at IS NULL")
                    if any(json.loads(row["payload"]).get("owner_id") == plan_id for row in active):
                        raise Conflict("legacy applying plan has an unverifiable owner; reconcile it explicitly")
            elif status not in {"planned", "partial"}:
                raise Conflict("storage plan is not executable", detail={"status": status})
            data.update(status="applying", updated_at=iso(now_utc()))
            if owner is not None:
                data["execution"] = _copy(owner)
            self._conn.execute("UPDATE storage_plans SET payload=? WHERE plan_id=?", (_dump(data), plan_id))
            return {**data, "_claimed": True, "_previous_status": previous}

    def release_guard(self, guard_id: str) -> None:
        with self._transaction():
            row = self._conn.execute(
                "SELECT payload FROM storage_guards WHERE guard_id=? AND released_at IS NULL", (guard_id,),
            ).fetchone()
            if row is None:
                return
            data = json.loads(row["payload"])
            data.update(released_at=iso(now_utc()), status="released")
            self._conn.execute(
                "UPDATE storage_guards SET released_at=?, payload=? WHERE guard_id=?",
                (data["released_at"], _dump(data), guard_id),
            )

    def guards(self, node_id: str | None = None) -> list[dict[str, Any]]:
        """Return active guards only; released records remain persisted for recovery."""
        clause, params = self._where(node_id=node_id)
        clause += " AND released_at IS NULL" if clause else " WHERE released_at IS NULL"
        return self._rows("SELECT payload FROM storage_guards" + clause + " ORDER BY seq", params)

    @contextmanager
    def guard(self, node_id: str, roots: list[str], *, operation: str, owner_id: str) -> Iterator[str]:
        """Release on normal completion only; exceptions preserve uncertain work."""
        guard_id = self.acquire_guard(node_id, roots, operation=operation, owner_id=owner_id)
        yield guard_id
        self.release_guard(guard_id)
