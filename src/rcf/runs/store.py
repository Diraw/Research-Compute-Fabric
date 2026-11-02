"""SQLite persistence: plans, runs, events, GPU leases, audit trail.

The registry (nodes/projects/environments) is *declarative* and lives in TOML;
the database only holds dynamic state that must survive an Agent session:
what was planned, what was submitted, what happened, and who did it.

Concurrency model: one connection per process guarded by a re-entrant lock,
WAL journal and a busy timeout.  RCF is a single-user control plane; it does not
try to be a multi-writer server (that is what the roadmap's web API is for).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import Conflict, StoreError
from ..core.ids import iso, now_utc, parse_iso
from ..core.models import (
    InputManifest,
    JobHandle,
    PlannedCommand,
    RunEvent,
    RunPlan,
    RunRecord,
    RunState,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    node_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    plan_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT,
    project_id TEXT,
    node_id TEXT NOT NULL,
    executor TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    params_json TEXT NOT NULL DEFAULT '{}',
    command_json TEXT,
    workdir TEXT,
    log_path TEXT,
    result_dir TEXT,
    env_id TEXT,
    env_fingerprint TEXT,
    handle_json TEXT,
    idempotency_key TEXT,
    error TEXT,
    last_observed_at TEXT,
    contract_violation INTEGER NOT NULL DEFAULT 0,
    git_commit TEXT,
    git_dirty INTEGER NOT NULL DEFAULT 0,
    inputs_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_idempotency
    ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_runs_state ON runs(state);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_run ON run_events(run_id, id);

CREATE TABLE IF NOT EXISTS gpu_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    gpu_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lease_active
    ON gpu_leases(node_id, gpu_key) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_lease_run ON gpu_leases(run_id);

CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    tier TEXT NOT NULL,
    decision TEXT NOT NULL,
    node_id TEXT,
    run_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audits_at ON audits(at);

CREATE TABLE IF NOT EXISTS managed_prefixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    path TEXT NOT NULL,
    env_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_managed_prefix ON managed_prefixes(node_id, path);

CREATE TABLE IF NOT EXISTS environment_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    env_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_env_verification
    ON environment_verifications(env_id, node_id, fingerprint);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        except sqlite3.Error as exc:  # pragma: no cover - filesystem issue
            raise StoreError(f"cannot open database {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``; checking ``table_info`` keeps
        this idempotent and safe for databases written by older versions.
        """

        existing = {row["name"] for row in self._all("PRAGMA table_info(runs)")}
        additions = {
            "contract_violation": "ALTER TABLE runs ADD COLUMN contract_violation INTEGER NOT NULL DEFAULT 0",
            "git_commit": "ALTER TABLE runs ADD COLUMN git_commit TEXT",
            "git_dirty": "ALTER TABLE runs ADD COLUMN git_dirty INTEGER NOT NULL DEFAULT 0",
            "inputs_json": "ALTER TABLE runs ADD COLUMN inputs_json TEXT",
        }
        for column, statement in additions.items():
            if column not in existing:
                self._conn.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- plans -------------------------------------------------------------- #
    def save_plan(self, plan: RunPlan) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO plans(plan_id, created_at, node_id, project_id, fingerprint, plan_json)"
                " VALUES (?,?,?,?,?,?)",
                (
                    plan.plan_id,
                    iso(plan.created_at),
                    plan.node_id,
                    plan.request.project_id,
                    plan.fingerprint,
                    plan.model_dump_json(),
                ),
            )
            self._conn.commit()

    def get_plan(self, plan_id: str) -> RunPlan | None:
        row = self._one("SELECT plan_json FROM plans WHERE plan_id = ?", (plan_id,))
        return RunPlan.model_validate_json(row["plan_json"]) if row else None

    # -- runs --------------------------------------------------------------- #
    def insert_run(self, record: RunRecord) -> None:
        payload = self._run_row(record)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO runs(run_id, plan_id, project_id, node_id, executor, state, created_at,"
                    " submitted_at, finished_at, exit_code, params_json, command_json, workdir, log_path,"
                    " result_dir, env_id, env_fingerprint, handle_json, idempotency_key, error, last_observed_at,"
                    " contract_violation, git_commit, git_dirty, inputs_json)"
                    " VALUES (:run_id, :plan_id, :project_id, :node_id, :executor, :state, :created_at,"
                    " :submitted_at, :finished_at, :exit_code, :params_json, :command_json, :workdir, :log_path,"
                    " :result_dir, :env_id, :env_fingerprint, :handle_json, :idempotency_key, :error,"
                    " :last_observed_at, :contract_violation, :git_commit, :git_dirty, :inputs_json)",
                    payload,
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                if "idempotency" in str(exc):
                    raise Conflict(
                        "a run with this idempotency key already exists",
                        hint="query it with run.list/get instead of submitting again",
                        detail={"idempotency_key": record.idempotency_key},
                    ) from exc
                raise StoreError(f"cannot insert run: {exc}") from exc

    def claim_submission(
        self, record: RunRecord, *, keys: Iterable[str] = ()
    ) -> tuple[RunRecord, bool]:
        """Claim dispatch and reserve GPUs in one cross-process transaction.

        The winner alone may call an executor. Existing records, including
        failed and unknown submissions, are never dispatched again. No network
        operation may run while this short transaction holds the write lock.
        """

        keys = list(dict.fromkeys(keys))
        payload = self._run_row(record)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if record.idempotency_key:
                    existing = self.find_by_idempotency(record.idempotency_key)
                    if existing is not None:
                        old = self.get_plan(existing.plan_id) if existing.plan_id else None
                        new = self.get_plan(record.plan_id) if record.plan_id else None
                        if old is None or new is None or old.fingerprint != new.fingerprint:
                            raise Conflict(
                                "idempotency key was already used for different work",
                                detail={"idempotency_key": record.idempotency_key, "existing_run": existing.run_id},
                            )
                        self._conn.commit()
                        return existing, False
                existing = self.get_run(record.run_id)
                if existing is not None:
                    self._conn.commit()
                    return existing, False
                for key in keys:
                    self._conn.execute(
                        "INSERT INTO gpu_leases(run_id, node_id, gpu_key, created_at) VALUES (?,?,?,?)",
                        (record.run_id, record.node_id, key, iso(now_utc())),
                    )
                columns = ", ".join(payload)
                values = ", ".join(":" + key for key in payload)
                self._conn.execute(f"INSERT INTO runs ({columns}) VALUES ({values})", payload)
                self._conn.commit()
                return record, True
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                taken = self.active_leases(record.node_id)
                raise Conflict(
                    "submission conflicts with an existing run or GPU reservation",
                    hint="inspect the existing run or create a new plan; do not blindly retry",
                    detail={"run_id": record.run_id, "held_by": {k: taken[k] for k in keys if k in taken}},
                ) from exc
            except Exception:
                self._conn.rollback()
                raise

    def update_run(self, record: RunRecord) -> bool:
        """Save a live record only; a persisted terminal result cannot regress.

        The condition is evaluated by SQLite, not a read-before-write check.
        Terminal persistence and lease release are one transaction so a crash
        cannot leave an active-looking run with its reservation released.
        """

        payload = self._run_row(record)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET state=:state, submitted_at=:submitted_at, finished_at=:finished_at,"
                " exit_code=:exit_code, command_json=:command_json, workdir=:workdir, log_path=:log_path,"
                " result_dir=:result_dir, handle_json=:handle_json, error=:error,"
                " last_observed_at=:last_observed_at, env_fingerprint=:env_fingerprint,"
                " contract_violation=:contract_violation, git_commit=:git_commit, git_dirty=:git_dirty,"
                " inputs_json=:inputs_json"
                " WHERE run_id=:run_id AND state NOT IN ('succeeded','failed','cancelled','lost')",
                payload,
            )
            saved = cursor.rowcount == 1
            if saved and record.state.terminal:
                self._conn.execute(
                    "UPDATE gpu_leases SET released_at=? WHERE run_id=? AND released_at IS NULL",
                    (iso(now_utc()), record.run_id),
                )
            self._conn.commit()
            return saved

    def record_observation_error(self, run_id: str, error: str) -> None:
        """An unsuccessful probe changes metadata, never the saved run state."""

        with self._lock:
            self._conn.execute(
                "UPDATE runs SET error=?, last_observed_at=? WHERE run_id=? "
                "AND state NOT IN ('succeeded','failed','cancelled','lost')",
                (error, iso(now_utc()), run_id),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return self._to_record(row) if row else None

    def find_by_idempotency(self, key: str) -> RunRecord | None:
        row = self._one("SELECT * FROM runs WHERE idempotency_key = ?", (key,))
        return self._to_record(row) if row else None

    def list_runs(self, *, limit: int = 50, state: RunState | None = None) -> list[RunRecord]:
        if state is None:
            rows = self._all("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            rows = self._all(
                "SELECT * FROM runs WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (state.value, limit),
            )
        return [self._to_record(row) for row in rows]

    def active_runs(self) -> list[RunRecord]:
        rows = self._all(
            "SELECT * FROM runs WHERE state IN ('planned','submitting','pending','running','unknown')"
            " ORDER BY created_at ASC"
        )
        return [self._to_record(row) for row in rows]

    # -- events ------------------------------------------------------------- #
    def add_event(self, event: RunEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_events(run_id, at, kind, message, data_json) VALUES (?,?,?,?,?)",
                (
                    event.run_id,
                    iso(event.at),
                    event.kind,
                    event.message,
                    json.dumps(event.data, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()

    def list_events(self, run_id: str, *, limit: int = 100) -> list[RunEvent]:
        rows = self._all(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY id ASC LIMIT ?", (run_id, limit)
        )
        return [
            RunEvent(
                run_id=row["run_id"],
                at=parse_iso(row["at"]) or now_utc(),
                kind=row["kind"],
                message=row["message"],
                data=json.loads(row["data_json"] or "{}"),
            )
            for row in rows
        ]

    # -- GPU leases --------------------------------------------------------- #
    def acquire_leases(self, *, run_id: str, node_id: str, keys: Iterable[str]) -> list[str]:
        """Reserve GPU keys atomically; raises :class:`Conflict` if any is taken."""

        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        stamp = iso(now_utc())
        with self._lock:
            try:
                for key in keys:
                    self._conn.execute(
                        "INSERT INTO gpu_leases(run_id, node_id, gpu_key, created_at) VALUES (?,?,?,?)",
                        (run_id, node_id, key, stamp),
                    )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                taken = self.active_leases(node_id)
                raise Conflict(
                    f"GPU {keys} on node {node_id!r} is already reserved by an RCF run",
                    hint="choose another GPU/node, wait for the other run, or set allow_shared_gpu explicitly",
                    detail={
                        "node_id": node_id,
                        "requested": keys,
                        "held_by": {k: taken.get(k) for k in keys if k in taken},
                    },
                ) from exc
        return keys

    def active_leases(self, node_id: str | None = None) -> dict[str, str]:
        if node_id is None:
            rows = self._all("SELECT node_id, gpu_key, run_id FROM gpu_leases WHERE released_at IS NULL")
            return {f"{row['node_id']}:{row['gpu_key']}": row["run_id"] for row in rows}
        rows = self._all(
            "SELECT gpu_key, run_id FROM gpu_leases WHERE released_at IS NULL AND node_id = ?",
            (node_id,),
        )
        return {row["gpu_key"]: row["run_id"] for row in rows}

    def release_leases(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE gpu_leases SET released_at = ? WHERE run_id = ? AND released_at IS NULL",
                (iso(now_utc()), run_id),
            )
            self._conn.commit()

    # -- audit -------------------------------------------------------------- #
    def record_audit(
        self,
        *,
        actor: str,
        operation: str,
        tier: str,
        decision: str,
        node_id: str | None = None,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audits(at, actor, operation, tier, decision, node_id, run_id, detail_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    iso(now_utc()),
                    actor,
                    operation,
                    tier,
                    decision,
                    node_id,
                    run_id,
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()

    def list_audits(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "at": row["at"],
                "actor": row["actor"],
                "operation": row["operation"],
                "tier": row["tier"],
                "decision": row["decision"],
                "node_id": row["node_id"],
                "run_id": row["run_id"],
                "detail": json.loads(row["detail_json"] or "{}"),
            }
            for row in rows
        ]

    # -- environment verification ------------------------------------------- #
    def record_verification(
        self, *, env_id: str, node_id: str, fingerprint: str, detail: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO environment_verifications"
                "(env_id, node_id, fingerprint, verified_at, detail_json) VALUES (?,?,?,?,?)",
                (env_id, node_id, fingerprint, iso(now_utc()), json.dumps(detail, default=str)),
            )
            self._conn.commit()

    def get_verification(self, *, env_id: str, node_id: str, fingerprint: str) -> datetime | None:
        row = self._one(
            "SELECT verified_at FROM environment_verifications"
            " WHERE env_id=? AND node_id=? AND fingerprint=?",
            (env_id, node_id, fingerprint),
        )
        return parse_iso(row["verified_at"]) if row else None

    # -- managed environment prefixes --------------------------------------- #
    def record_managed_prefix(self, *, node_id: str, path: str, env_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO managed_prefixes(node_id, path, env_id, created_at)"
                " VALUES (?,?,?,?)",
                (node_id, path, env_id, iso(now_utc())),
            )
            self._conn.commit()

    def is_managed_prefix(self, *, node_id: str, path: str) -> bool:
        row = self._one(
            "SELECT 1 FROM managed_prefixes WHERE node_id = ? AND path = ?", (node_id, path)
        )
        return row is not None

    # -- internals ---------------------------------------------------------- #
    def _one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _run_row(record: RunRecord) -> dict[str, Any]:
        return {
            "run_id": record.run_id,
            "plan_id": record.plan_id,
            "project_id": record.project_id,
            "node_id": record.node_id,
            "executor": record.executor,
            "state": record.state.value,
            "created_at": iso(record.created_at),
            "submitted_at": iso(record.submitted_at),
            "finished_at": iso(record.finished_at),
            "exit_code": record.exit_code,
            "params_json": json.dumps(record.params, ensure_ascii=False, default=str),
            "command_json": record.command.model_dump_json() if record.command else None,
            "workdir": record.workdir,
            "log_path": record.log_path,
            "result_dir": record.result_dir,
            "env_id": record.env_id,
            "env_fingerprint": record.env_fingerprint,
            "handle_json": record.handle.model_dump_json() if record.handle else None,
            "idempotency_key": record.idempotency_key,
            "error": record.error,
            "last_observed_at": iso(record.last_observed_at),
            "contract_violation": 1 if record.contract_violation else 0,
            "git_commit": record.git_commit,
            "git_dirty": 1 if record.git_dirty else 0,
            "inputs_json": record.inputs.model_dump_json() if record.inputs else None,
        }

    @staticmethod
    def _to_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            plan_id=row["plan_id"],
            project_id=row["project_id"],
            node_id=row["node_id"],
            executor=row["executor"],
            state=RunState(row["state"]),
            created_at=parse_iso(row["created_at"]) or now_utc(),
            submitted_at=parse_iso(row["submitted_at"]),
            finished_at=parse_iso(row["finished_at"]),
            exit_code=row["exit_code"],
            params=json.loads(row["params_json"] or "{}"),
            command=PlannedCommand.model_validate_json(row["command_json"]) if row["command_json"] else None,
            workdir=row["workdir"],
            log_path=row["log_path"],
            result_dir=row["result_dir"],
            env_id=row["env_id"],
            env_fingerprint=row["env_fingerprint"],
            handle=JobHandle.model_validate_json(row["handle_json"]) if row["handle_json"] else None,
            idempotency_key=row["idempotency_key"],
            error=row["error"],
            last_observed_at=parse_iso(row["last_observed_at"]),
            contract_violation=bool(row["contract_violation"]),
            git_commit=row["git_commit"],
            git_dirty=bool(row["git_dirty"]),
            inputs=InputManifest.model_validate_json(row["inputs_json"]) if row["inputs_json"] else None,
        )


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
