"""Small durable SQLite store for the standalone reference deployment.

The store intentionally owns only judge state. A production platform may replace
it with its own adapter or a service-backed implementation without changing the
public execution contracts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from brunost_judge.contracts import ExecutionRequest, ExecutionResult, TaskRecord


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JudgeStore:
    def __init__(self, database: str | Path = "judge.db") -> None:
        self.path = str(database)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        if self.path != ":memory:":
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
                    cancel_requested INTEGER NOT NULL DEFAULT 0
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
                """
            )

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

    def submit(self, request: ExecutionRequest) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        now = _now()
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT * FROM executions WHERE idempotency_key = ?", (request.idempotency_key,)
            ).fetchone()
            if existing is not None:
                return self._result(existing)
            if db.execute("SELECT 1 FROM tasks WHERE task_ref = ?", (request.task_ref,)).fetchone() is None:
                raise KeyError(f"unknown task_ref: {request.task_ref}")
            db.execute(
                """INSERT INTO executions(
                    execution_id,idempotency_key,task_ref,submission_path,callback_url,
                    callback_token,metadata_json,status,metrics_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    request.idempotency_key,
                    request.task_ref,
                    request.submission_path,
                    request.callback_url,
                    request.callback_token,
                    json.dumps(request.metadata, sort_keys=True),
                    "queued",
                    "{}",
                    now,
                    now,
                ),
            )
        return self.get_execution(execution_id)  # type: ignore[return-value]

    def claim_next(self) -> tuple[ExecutionResult, TaskRecord, dict[str, Any]] | None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM executions WHERE status = 'queued' AND cancel_requested = 0 ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now = _now()
            db.execute("UPDATE executions SET status='running', updated_at=? WHERE execution_id=?", (now, row["execution_id"]))
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
        }

    def finish(self, execution_id: str, result: ExecutionResult) -> ExecutionResult:
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE executions SET status=?,score=?,metrics_json=?,failure_reason=?,
                   metadata_json=?,updated_at=? WHERE execution_id=?""",
                (
                    result.status,
                    result.score,
                    json.dumps(result.metrics, sort_keys=True),
                    result.failure_reason,
                    json.dumps(result.metadata, sort_keys=True),
                    _now(),
                    execution_id,
                ),
            )
        return self.get_execution(execution_id)  # type: ignore[return-value]

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
                ((datetime.now(UTC) + timedelta(seconds=5)).isoformat(), error[:2000], execution_id),
            )

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

    def _result(self, row: sqlite3.Row) -> ExecutionResult:
        return ExecutionResult(
            execution_id=row["execution_id"],
            task_ref=row["task_ref"],
            status=row["status"],
            score=row["score"],
            metrics=json.loads(row["metrics_json"]),
            failure_reason=row["failure_reason"],
            metadata=json.loads(row["metadata_json"]),
        )
