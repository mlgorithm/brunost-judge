"""Optional PostgreSQL store for multi-node deployments.

Install ``brunost-judge[production]`` to enable this adapter. It deliberately
implements the same small store surface as the SQLite reference store, so
platform integrations do not depend on a database-specific API.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from brunost_judge.contracts import ExecutionRequest, ExecutionResult, TaskRecord


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PostgresJudgeStore:
    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("Install brunost-judge[production] for PostgreSQL support") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._initialize()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    task_ref TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
                    manifest_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    task_ref TEXT NOT NULL REFERENCES tasks(task_ref), submission_path TEXT NOT NULL,
                    callback_url TEXT, callback_token TEXT, metadata_json JSONB NOT NULL,
                    status TEXT NOT NULL, score DOUBLE PRECISION, metrics_json JSONB NOT NULL,
                    failure_reason TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE, queue TEXT NOT NULL DEFAULT 'default',
                    resource_class TEXT NOT NULL DEFAULT 'cpu', priority INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT, lease_expires_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS ix_executions_queue ON executions(status, priority DESC, created_at);
                CREATE TABLE IF NOT EXISTS callback_deliveries (
                    execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id), callback_url TEXT NOT NULL,
                    callback_token TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL,
                    delivered_at TIMESTAMPTZ, last_error TEXT
                );"""
            )

    def register_task(self, task: TaskRecord) -> TaskRecord:
        with self._connect() as db:
            db.execute(
                """INSERT INTO tasks(task_ref,path,kind,manifest_json,created_at) VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(task_ref) DO UPDATE SET path=EXCLUDED.path,kind=EXCLUDED.kind,manifest_json=EXCLUDED.manifest_json""",
                (task.task_ref, task.path, task.kind, json.dumps(task.manifest), _now()),
            )
        return task

    def get_task(self, task_ref: str) -> TaskRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_ref=%s", (task_ref,)).fetchone()
        return self._task(row) if row else None

    def list_tasks(self) -> list[TaskRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY task_ref").fetchall()
        return [self._task(row) for row in rows]

    def submit(self, request: ExecutionRequest) -> ExecutionResult:
        execution_id, now = str(uuid.uuid4()), _now()
        with self._connect() as db:
            existing = db.execute("SELECT * FROM executions WHERE idempotency_key=%s", (request.idempotency_key,)).fetchone()
            if existing:
                return self._result(existing)
            if not db.execute("SELECT 1 FROM tasks WHERE task_ref=%s", (request.task_ref,)).fetchone():
                raise KeyError(f"unknown task_ref: {request.task_ref}")
            db.execute(
                """INSERT INTO executions(execution_id,idempotency_key,task_ref,submission_path,callback_url,callback_token,
                   metadata_json,status,metrics_json,created_at,updated_at,queue,resource_class,priority)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (execution_id, request.idempotency_key, request.task_ref, request.submission_path, request.callback_url,
                 request.callback_token, json.dumps(request.metadata), "queued", json.dumps({}), now, now,
                 request.queue, request.resource_class, request.priority),
            )
        return self.get_execution(execution_id)  # type: ignore[return-value]

    def claim_next(self, *, worker_id: str = "local-worker", queues: tuple[str, ...] | None = None,
                   resource_classes: tuple[str, ...] | None = None, lease_seconds: int = 300):
        with self._connect() as db:
            now = datetime.now(UTC)
            db.execute("UPDATE executions SET status='queued',worker_id=NULL,lease_expires_at=NULL,updated_at=%s WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<=%s", (now, now))
            clauses, params = ["status='queued'", "cancel_requested=FALSE"], []
            if queues:
                clauses.append("queue = ANY(%s)"); params.append(list(queues))
            if resource_classes:
                clauses.append("resource_class = ANY(%s)"); params.append(list(resource_classes))
            row = db.execute("SELECT * FROM executions WHERE " + " AND ".join(clauses) + " ORDER BY priority DESC,created_at LIMIT 1 FOR UPDATE SKIP LOCKED", params).fetchone()
            if not row:
                return None
            lease = now + timedelta(seconds=max(1, lease_seconds))
            db.execute("UPDATE executions SET status='running',worker_id=%s,lease_expires_at=%s,updated_at=%s WHERE execution_id=%s", (worker_id, lease, now, row["execution_id"]))
            task_row = db.execute("SELECT * FROM tasks WHERE task_ref=%s", (row["task_ref"],)).fetchone()
        if not task_row:
            return None
        return self.get_execution(row["execution_id"]), self._task(task_row), {"callback_url": row["callback_url"], "callback_token": row["callback_token"], "submission_path": row["submission_path"], "queue": row["queue"], "resource_class": row["resource_class"]}

    def finish(self, execution_id: str, result: ExecutionResult) -> ExecutionResult:
        with self._connect() as db:
            db.execute("UPDATE executions SET status=%s,score=%s,metrics_json=%s,failure_reason=%s,metadata_json=%s,worker_id=NULL,lease_expires_at=NULL,updated_at=%s WHERE execution_id=%s", (result.status, result.score, json.dumps(result.metrics), result.failure_reason, json.dumps(result.metadata), _now(), execution_id))
        return self.get_execution(execution_id)  # type: ignore[return-value]

    def enqueue_callback(self, execution_id: str, callback_url: str, callback_token: str | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO callback_deliveries(execution_id,callback_url,callback_token,next_attempt_at) VALUES(%s,%s,%s,%s) ON CONFLICT(execution_id) DO UPDATE SET callback_url=EXCLUDED.callback_url,callback_token=EXCLUDED.callback_token", (execution_id, callback_url, callback_token, _now()))

    def pending_callbacks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT c.*,e.status,e.task_ref,e.score,e.metrics_json,e.failure_reason,e.metadata_json FROM callback_deliveries c JOIN executions e USING(execution_id) WHERE c.delivered_at IS NULL AND c.next_attempt_at<=%s ORDER BY c.next_attempt_at LIMIT %s", (_now(), limit)).fetchall()
        return [dict(row) for row in rows]

    def mark_callback_delivered(self, execution_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE callback_deliveries SET delivered_at=%s,last_error=NULL WHERE execution_id=%s", (_now(), execution_id))

    def mark_callback_failed(self, execution_id: str, error: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT attempts FROM callback_deliveries WHERE execution_id=%s", (execution_id,)).fetchone()
            attempts = int(row["attempts"]) if row else 0
            db.execute("UPDATE callback_deliveries SET attempts=attempts+1,next_attempt_at=%s,last_error=%s WHERE execution_id=%s", (datetime.now(UTC) + timedelta(seconds=min(3600, 5 * (2 ** min(8, attempts)))), error[:2000], execution_id))

    def cancel(self, execution_id: str) -> ExecutionResult | None:
        with self._connect() as db:
            db.execute("UPDATE executions SET cancel_requested=TRUE,status=CASE WHEN status='queued' THEN 'canceled' ELSE status END,updated_at=%s WHERE execution_id=%s", (_now(), execution_id))
        return self.get_execution(execution_id)

    def get_execution(self, execution_id: str) -> ExecutionResult | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM executions WHERE execution_id=%s", (execution_id,)).fetchone()
        return self._result(row) if row else None

    def list_executions(self, *, status: str | None = None, limit: int = 100) -> list[ExecutionResult]:
        limit = max(1, min(1000, int(limit)))
        with self._connect() as db:
            if status:
                rows = db.execute("SELECT * FROM executions WHERE status=%s ORDER BY created_at DESC LIMIT %s", (status, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM executions ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return [self._result(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status,COUNT(*) AS count FROM executions GROUP BY status").fetchall()
        result = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "canceled": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    @staticmethod
    def _task(row: dict[str, Any]) -> TaskRecord:
        manifest = row["manifest_json"] if isinstance(row["manifest_json"], dict) else json.loads(row["manifest_json"])
        return TaskRecord(row["task_ref"], row["path"], row["kind"], manifest)

    @staticmethod
    def _result(row: dict[str, Any]) -> ExecutionResult:
        metrics = row["metrics_json"] if isinstance(row["metrics_json"], dict) else json.loads(row["metrics_json"])
        metadata = row["metadata_json"] if isinstance(row["metadata_json"], dict) else json.loads(row["metadata_json"])
        return ExecutionResult(row["execution_id"], row["task_ref"], row["status"], row["score"], metrics, row["failure_reason"], metadata, queue=row["queue"], resource_class=row["resource_class"], priority=row["priority"])
