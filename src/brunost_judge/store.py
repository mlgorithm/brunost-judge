"""Small durable SQLite store for the standalone reference deployment.

The store intentionally owns only judge state. A production platform may replace
it with its own adapter or a service-backed implementation without changing the
public execution contracts.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from brunost_judge.contracts import (
    ExecutionRequest,
    ExecutionResult,
    TaskRecord,
    WorkerRecord,
)
from brunost_judge.enrollment import digest_secret, is_expired


def create_store(database: str | Path = "judge.db"):
    """Create the configured durable store.

    SQLite remains the zero-dependency local default. A PostgreSQL URL selects
    the optional production adapter, allowing the same API and worker code to
    run against a shared multi-node control-plane database.
    """
    value = str(database)
    if value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        from brunost_judge.postgres_store import PostgresJudgeStore

        return PostgresJudgeStore(value)
    return JudgeStore(database)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JudgeStore:
    def __init__(self, database: str | Path = "judge.db") -> None:
        requested_path = str(database)
        self._sqlite_uri = requested_path == ":memory:"
        # Every sqlite3.connect(":memory:") call creates a different database.
        # Use a private shared-cache URI and keep one connection alive so the
        # store behaves like a real in-memory database across its connections.
        self.path = (
            f"file:brunost_judge_{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self._sqlite_uri
            else requested_path
        )
        self._lock = threading.RLock()
        self._memory_keeper: sqlite3.Connection | None = None
        if self._sqlite_uri:
            self._memory_keeper = self._connect()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False, uri=self._sqlite_uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        if not self._sqlite_uri:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_ref TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    task_ref TEXT NOT NULL REFERENCES tasks(task_ref),
                    submission_path TEXT NOT NULL,
                    callback_url TEXT,
                    callback_token TEXT,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL,
                    metrics_json TEXT NOT NULL,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    queue TEXT NOT NULL DEFAULT 'default',
                    resource_class TEXT NOT NULL DEFAULT 'cpu',
                    priority INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_executions_queue
                    ON executions(status, created_at);
                CREATE TABLE IF NOT EXISTS callback_deliveries (
                    execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id),
                    callback_url TEXT NOT NULL,
                    callback_token TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS definitions (
                    definition_type TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (definition_type, definition_id)
                );
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    capabilities_json TEXT NOT NULL,
                    queues_json TEXT NOT NULL,
                    resource_classes_json TEXT NOT NULL,
                    region TEXT,
                    status TEXT NOT NULL,
                    draining INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_enrollment_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_node_enrollment_tokens_active
                    ON node_enrollment_tokens(token_hash, used_at, expires_at);
                CREATE TABLE IF NOT EXISTS worker_credentials (
                    worker_id TEXT PRIMARY KEY REFERENCES workers(worker_id),
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                """
            )
            # Keep existing reference deployments upgradeable without a manual
            # migration command.  New installations get these columns from the
            # CREATE TABLE above; old SQLite files are extended in place.
            columns = {row[1] for row in db.execute("PRAGMA table_info(executions)")}
            for name, definition in (
                ("queue", "TEXT NOT NULL DEFAULT 'default'"),
                ("resource_class", "TEXT NOT NULL DEFAULT 'cpu'"),
                ("priority", "INTEGER NOT NULL DEFAULT 0"),
                ("worker_id", "TEXT"),
                ("lease_expires_at", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE executions ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS ix_executions_lease ON executions(status, lease_expires_at)")

    def register_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO tasks(task_ref,path,kind,manifest_json,created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(task_ref) DO UPDATE SET path=excluded.path,
                     kind=excluded.kind, manifest_json=excluded.manifest_json""",
                (task.task_ref, task.path, task.kind, json.dumps(task.manifest, sort_keys=True), _now()),
            )
        return task

    def get_task(self, task_ref: str) -> TaskRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_ref = ?", (task_ref,)).fetchone()
        if row is None:
            return None
        return TaskRecord(row["task_ref"], row["path"], row["kind"], json.loads(row["manifest_json"]))

    def list_tasks(self) -> list[TaskRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY task_ref").fetchall()
        return [TaskRecord(row["task_ref"], row["path"], row["kind"], json.loads(row["manifest_json"])) for row in rows]

    def register_definition(self, definition_type: str, definition_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Upsert an extensible agent/game definition without leaking DB details."""
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO definitions(definition_type,definition_id,payload_json,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(definition_type,definition_id) DO UPDATE SET
                   payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (definition_type, definition_id, json.dumps(payload, sort_keys=True), now, now),
            )
        return {"definition_type": definition_type, "definition_id": definition_id, **payload}

    def get_definition(self, definition_type: str, definition_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM definitions WHERE definition_type=? AND definition_id=?",
                (definition_type, definition_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "definition_type": row["definition_type"],
            "definition_id": row["definition_id"],
            **json.loads(row["payload_json"]),
        }

    def list_definitions(self, definition_type: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM definitions WHERE definition_type=? ORDER BY definition_id",
                (definition_type,),
            ).fetchall()
        return [
            {"definition_type": row["definition_type"], "definition_id": row["definition_id"], **json.loads(row["payload_json"])}
            for row in rows
        ]

    def register_worker(self, worker: WorkerRecord) -> WorkerRecord:
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO workers(worker_id,capabilities_json,queues_json,resource_classes_json,region,status,draining,metadata_json,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                   capabilities_json=excluded.capabilities_json,queues_json=excluded.queues_json,
                   resource_classes_json=excluded.resource_classes_json,region=excluded.region,status=excluded.status,
                   draining=excluded.draining,metadata_json=excluded.metadata_json,last_seen=excluded.last_seen""",
                (
                    worker.worker_id,
                    json.dumps(sorted(worker.capabilities)),
                    json.dumps(sorted(worker.queues)),
                    json.dumps(sorted(worker.resource_classes)),
                    worker.region,
                    worker.status,
                    int(worker.draining),
                    json.dumps(worker.metadata, sort_keys=True),
                    now,
                ),
            )
        return self.get_worker(worker.worker_id)  # type: ignore[return-value]

    def get_worker(self, worker_id: str) -> WorkerRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
        return self._worker(row) if row else None

    def list_workers(self) -> list[WorkerRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
        return [self._worker(row) for row in rows]

    def heartbeat_worker(self, worker_id: str, *, status: str = "ready") -> WorkerRecord | None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE workers SET status=?,last_seen=? WHERE worker_id=?", (status, _now(), worker_id))
        return self.get_worker(worker_id)

    def drain_worker(self, worker_id: str, *, draining: bool = True) -> WorkerRecord | None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE workers SET draining=?,last_seen=? WHERE worker_id=?", (int(draining), _now(), worker_id))
        return self.get_worker(worker_id)

    def create_enrollment_token(
        self,
        *,
        token_id: str,
        token_hash: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> dict[str, Any]:
        """Persist a short-lived, single-use node enrollment token."""

        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO node_enrollment_tokens(token_id,token_hash,payload_json,expires_at,created_at)
                   VALUES(?,?,?,?,?)""",
                (token_id, token_hash, json.dumps(payload, sort_keys=True), expires_at, now),
            )
        return {"token_id": token_id, **payload, "expires_at": expires_at}

    def consume_enrollment_token(self, token: str) -> dict[str, Any] | None:
        """Atomically consume a valid enrollment token, returning its payload."""

        token_hash = digest_secret(token)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM node_enrollment_tokens WHERE token_hash=? AND used_at IS NULL",
                (token_hash,),
            ).fetchone()
            if row is None or is_expired(row["expires_at"]):
                db.rollback()
                return None
            db.execute(
                "UPDATE node_enrollment_tokens SET used_at=? WHERE token_id=? AND used_at IS NULL",
                (_now(), row["token_id"]),
            )
            db.commit()
        return {"token_id": row["token_id"], **json.loads(row["payload_json"]), "expires_at": row["expires_at"]}

    def create_worker_credential(self, worker_id: str, token: str) -> None:
        """Replace a worker credential; the raw token is never written to disk."""

        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO worker_credentials(worker_id,token_hash,created_at,revoked_at)
                   VALUES(?,?,?,NULL)
                   ON CONFLICT(worker_id) DO UPDATE SET token_hash=excluded.token_hash,
                   created_at=excluded.created_at,revoked_at=NULL""",
                (worker_id, digest_secret(token), _now()),
            )

    def verify_worker_token(self, worker_id: str, token: str) -> bool:
        """Verify a worker-scoped credential using constant-time comparison."""

        with self._connect() as db:
            row = db.execute(
                "SELECT token_hash FROM worker_credentials WHERE worker_id=? AND revoked_at IS NULL",
                (worker_id,),
            ).fetchone()
        return row is not None and hmac.compare_digest(str(row["token_hash"]), digest_secret(token))

    def revoke_worker_credential(self, worker_id: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE worker_credentials SET revoked_at=? WHERE worker_id=? AND revoked_at IS NULL",
                (_now(), worker_id),
            )
        return cursor.rowcount > 0

    def submit(self, request: ExecutionRequest) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        now = _now()
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT * FROM executions WHERE idempotency_key = ?", (request.idempotency_key,)
            ).fetchone()
            if existing is not None:
                return self._result(existing)
            task_row = db.execute("SELECT * FROM tasks WHERE task_ref = ?", (request.task_ref,)).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task_ref: {request.task_ref}")
            manifest = json.loads(task_row["manifest_json"])
            metadata = dict(request.metadata)
            metadata["task_digest"] = manifest.get("digest")
            metadata["evaluator"] = manifest.get("evaluator")
            metadata["runtime_image"] = manifest.get("runtime_image") or manifest.get("runtime")
            metadata["event_id"] = f"execution:{execution_id}:result"
            db.execute(
                """INSERT INTO executions(
                    execution_id,idempotency_key,task_ref,submission_path,callback_url,
                    callback_token,metadata_json,status,metrics_json,created_at,updated_at
                    ,queue,resource_class,priority
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    request.idempotency_key,
                    request.task_ref,
                    request.submission_path,
                    request.callback_url,
                    request.callback_token,
                    json.dumps(metadata, sort_keys=True),
                    "queued",
                    "{}",
                    now,
                    now,
                    request.queue,
                    request.resource_class,
                    request.priority,
                ),
            )
        return self.get_execution(execution_id)  # type: ignore[return-value]

    def claim_next(
        self,
        *,
        worker_id: str = "local-worker",
        queues: tuple[str, ...] | None = None,
        resource_classes: tuple[str, ...] | None = None,
        lease_seconds: int = 300,
    ) -> tuple[ExecutionResult, TaskRecord, dict[str, Any]] | None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            db.execute(
                """UPDATE executions SET status='queued', worker_id=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
                (now.isoformat(), now.isoformat()),
            )
            clauses = ["status = 'queued'", "cancel_requested = 0"]
            params: list[Any] = []
            if queues:
                clauses.append("queue IN (" + ",".join("?" for _ in queues) + ")")
                params.extend(queues)
            if resource_classes:
                clauses.append("resource_class IN (" + ",".join("?" for _ in resource_classes) + ")")
                params.extend(resource_classes)
            row = db.execute(
                "SELECT * FROM executions WHERE " + " AND ".join(clauses) +
                " ORDER BY priority DESC, created_at LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now_text = now.isoformat()
            lease = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
            db.execute(
                "UPDATE executions SET status='running', worker_id=?, lease_expires_at=?, updated_at=? WHERE execution_id=?",
                (worker_id, lease, now_text, row["execution_id"]),
            )
            task_row = db.execute("SELECT * FROM tasks WHERE task_ref=?", (row["task_ref"],)).fetchone()
            db.commit()
        if task_row is None:
            return None
        updated = self.get_execution(row["execution_id"])
        assert updated is not None
        return updated, TaskRecord(task_row["task_ref"], task_row["path"], task_row["kind"], json.loads(task_row["manifest_json"])), {
            "callback_url": row["callback_url"],
            "callback_token": row["callback_token"],
            "submission_path": row["submission_path"],
            "queue": row["queue"],
            "resource_class": row["resource_class"],
        }

    def finish(self, execution_id: str, result: ExecutionResult, *, worker_id: str | None = None) -> ExecutionResult | None:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """UPDATE executions SET status=?,score=?,metrics_json=?,failure_reason=?,
                   metadata_json=?,worker_id=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE execution_id=? AND (? IS NULL OR worker_id=?)""",
                (
                    result.status,
                    result.score,
                    json.dumps(result.metrics, sort_keys=True),
                    result.failure_reason,
                    json.dumps(result.metadata, sort_keys=True),
                    _now(),
                    execution_id,
                    worker_id,
                    worker_id,
                ),
            )
        if cursor.rowcount == 0:
            return None
        return self.get_execution(execution_id)

    def enqueue_callback(self, execution_id: str, callback_url: str, callback_token: str | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO callback_deliveries(execution_id,callback_url,callback_token,next_attempt_at)
                   VALUES(?,?,?,?) ON CONFLICT(execution_id) DO UPDATE SET
                   callback_url=excluded.callback_url, callback_token=excluded.callback_token""",
                (execution_id, callback_url, callback_token, _now()),
            )

    def pending_callbacks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.*, e.status, e.task_ref, e.score, e.metrics_json, e.failure_reason,
                   e.metadata_json FROM callback_deliveries c JOIN executions e USING(execution_id)
                   WHERE c.delivered_at IS NULL AND c.next_attempt_at <= ? ORDER BY c.next_attempt_at LIMIT ?""",
                (_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_callback_delivered(self, execution_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE callback_deliveries SET delivered_at=?,last_error=NULL WHERE execution_id=?", (_now(), execution_id))

    def mark_callback_failed(self, execution_id: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE callback_deliveries SET attempts=attempts+1,
                   next_attempt_at=?,last_error=? WHERE execution_id=?""",
                ((datetime.now(UTC) + timedelta(seconds=min(3600, 5 * (2 ** min(8, self._callback_attempts(db, execution_id)))))).isoformat(), error[:2000], execution_id),
            )

    @staticmethod
    def _callback_attempts(db: sqlite3.Connection, execution_id: str) -> int:
        row = db.execute("SELECT attempts FROM callback_deliveries WHERE execution_id=?", (execution_id,)).fetchone()
        return int(row[0]) if row else 0

    def cancel(self, execution_id: str) -> ExecutionResult | None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE executions SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'canceled' ELSE status END,updated_at=? WHERE execution_id=?",
                (_now(), execution_id),
            )
        return self.get_execution(execution_id)

    def get_execution(self, execution_id: str) -> ExecutionResult | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
        return self._result(row) if row is not None else None

    def list_executions(self, *, status: str | None = None, limit: int = 100) -> list[ExecutionResult]:
        limit = max(1, min(1000, int(limit)))
        with self._connect() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM executions WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM executions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._result(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS count FROM executions GROUP BY status").fetchall()
        result = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "canceled": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def _result(self, row: sqlite3.Row) -> ExecutionResult:
        metadata = json.loads(row["metadata_json"])
        return ExecutionResult(
            execution_id=row["execution_id"],
            task_ref=row["task_ref"],
            status=row["status"],
            score=row["score"],
            metrics=json.loads(row["metrics_json"]),
            failure_reason=row["failure_reason"],
            metadata=metadata,
            queue=row["queue"],
            resource_class=row["resource_class"],
            priority=row["priority"],
            task_digest=metadata.get("task_digest"),
            evaluator=metadata.get("evaluator"),
            runtime_image=metadata.get("runtime_image"),
            seed=metadata.get("seed"),
            event_id=metadata.get("event_id"),
        )

    @staticmethod
    def _worker(row: sqlite3.Row) -> WorkerRecord:
        return WorkerRecord(
            worker_id=row["worker_id"],
            capabilities=tuple(json.loads(row["capabilities_json"])),
            queues=tuple(json.loads(row["queues_json"])),
            resource_classes=tuple(json.loads(row["resource_classes_json"])),
            region=row["region"],
            status=row["status"],
            draining=bool(row["draining"]),
            metadata=json.loads(row["metadata_json"]),
        )
