"""Optional PostgreSQL store for multi-node deployments.

Install ``brunost-judge[production]`` to enable this adapter. It deliberately
implements the same small store surface as the SQLite reference store, so
platform integrations do not depend on a database-specific API.
"""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from brunost_judge.contracts import (
    ExecutionRequest,
    ExecutionResult,
    TaskRecord,
    WorkerRecord,
)
from brunost_judge.enrollment import digest_secret, is_expired


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
                    scores_json JSONB NOT NULL DEFAULT '{}'::jsonb, winner TEXT,
                    artifacts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
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
                );
                CREATE TABLE IF NOT EXISTS definitions (
                    definition_type TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    payload_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (definition_type, definition_id)
                );
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    capabilities_json JSONB NOT NULL,
                    queues_json JSONB NOT NULL,
                    resource_classes_json JSONB NOT NULL,
                    region TEXT,
                    status TEXT NOT NULL,
                    draining BOOLEAN NOT NULL DEFAULT FALSE,
                    metadata_json JSONB NOT NULL,
                    last_seen TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_enrollment_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    payload_json JSONB NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_node_enrollment_tokens_active
                    ON node_enrollment_tokens(token_hash, used_at, expires_at);
                CREATE TABLE IF NOT EXISTS worker_credentials (
                    worker_id TEXT PRIMARY KEY REFERENCES workers(worker_id),
                    token_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    revoked_at TIMESTAMPTZ
                );
                ALTER TABLE executions ADD COLUMN IF NOT EXISTS scores_json JSONB NOT NULL DEFAULT '{}'::jsonb;
                ALTER TABLE executions ADD COLUMN IF NOT EXISTS winner TEXT;
                ALTER TABLE executions ADD COLUMN IF NOT EXISTS artifacts_json JSONB NOT NULL DEFAULT '{}'::jsonb;"""
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

    def register_definition(self, definition_type: str, definition_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO definitions(definition_type,definition_id,payload_json,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(definition_type,definition_id) DO UPDATE SET
                   payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at""",
                (definition_type, definition_id, json.dumps(payload), now, now),
            )
        return {"definition_type": definition_type, "definition_id": definition_id, **payload}

    def get_definition(self, definition_type: str, definition_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM definitions WHERE definition_type=%s AND definition_id=%s",
                (definition_type, definition_id),
            ).fetchone()
        if not row:
            return None
        payload = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
        return {"definition_type": row["definition_type"], "definition_id": row["definition_id"], **payload}

    def list_definitions(self, definition_type: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM definitions WHERE definition_type=%s ORDER BY definition_id",
                (definition_type,),
            ).fetchall()
        return [
            {
                "definition_type": row["definition_type"],
                "definition_id": row["definition_id"],
                **(row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])),
            }
            for row in rows
        ]

    def register_worker(self, worker: WorkerRecord) -> WorkerRecord:
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO workers(worker_id,capabilities_json,queues_json,resource_classes_json,region,status,draining,metadata_json,last_seen)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(worker_id) DO UPDATE SET capabilities_json=EXCLUDED.capabilities_json,
                   queues_json=EXCLUDED.queues_json,resource_classes_json=EXCLUDED.resource_classes_json,
                   region=EXCLUDED.region,status=EXCLUDED.status,draining=EXCLUDED.draining,
                   metadata_json=EXCLUDED.metadata_json,last_seen=EXCLUDED.last_seen""",
                (
                    worker.worker_id,
                    json.dumps(list(worker.capabilities)),
                    json.dumps(list(worker.queues)),
                    json.dumps(list(worker.resource_classes)),
                    worker.region,
                    worker.status,
                    worker.draining,
                    json.dumps(worker.metadata),
                    now,
                ),
            )
        return self.get_worker(worker.worker_id)  # type: ignore[return-value]

    def get_worker(self, worker_id: str) -> WorkerRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM workers WHERE worker_id=%s", (worker_id,)).fetchone()
        return self._worker(row) if row else None

    def list_workers(self) -> list[WorkerRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
        return [self._worker(row) for row in rows]

    def heartbeat_worker(self, worker_id: str, *, status: str = "ready") -> WorkerRecord | None:
        with self._connect() as db:
            db.execute("UPDATE workers SET status=%s,last_seen=%s WHERE worker_id=%s", (status, _now(), worker_id))
        return self.get_worker(worker_id)

    def drain_worker(self, worker_id: str, *, draining: bool = True) -> WorkerRecord | None:
        with self._connect() as db:
            db.execute("UPDATE workers SET draining=%s,last_seen=%s WHERE worker_id=%s", (draining, _now(), worker_id))
        return self.get_worker(worker_id)

    def create_enrollment_token(
        self,
        *,
        token_id: str,
        token_hash: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO node_enrollment_tokens(token_id,token_hash,payload_json,expires_at,created_at)
                   VALUES(%s,%s,%s,%s,%s)""",
                (token_id, token_hash, json.dumps(payload), expires_at, now),
            )
        return {"token_id": token_id, **payload, "expires_at": expires_at}

    def consume_enrollment_token(self, token: str) -> dict[str, Any] | None:
        token_hash = digest_secret(token)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM node_enrollment_tokens WHERE token_hash=%s AND used_at IS NULL FOR UPDATE",
                (token_hash,),
            ).fetchone()
            if row is None or is_expired(str(row["expires_at"])):
                return None
            db.execute(
                "UPDATE node_enrollment_tokens SET used_at=%s WHERE token_id=%s AND used_at IS NULL",
                (_now(), row["token_id"]),
            )
        payload = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
        return {"token_id": row["token_id"], **payload, "expires_at": row["expires_at"].isoformat() if hasattr(row["expires_at"], "isoformat") else str(row["expires_at"])}

    def create_worker_credential(self, worker_id: str, token: str) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO worker_credentials(worker_id,token_hash,created_at,revoked_at)
                   VALUES(%s,%s,%s,NULL)
                   ON CONFLICT(worker_id) DO UPDATE SET token_hash=EXCLUDED.token_hash,
                   created_at=EXCLUDED.created_at,revoked_at=NULL""",
                (worker_id, digest_secret(token), _now()),
            )

    def verify_worker_token(self, worker_id: str, token: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT token_hash FROM worker_credentials WHERE worker_id=%s AND revoked_at IS NULL",
                (worker_id,),
            ).fetchone()
        return row is not None and hmac.compare_digest(str(row["token_hash"]), digest_secret(token))

    def revoke_worker_credential(self, worker_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE worker_credentials SET revoked_at=%s WHERE worker_id=%s AND revoked_at IS NULL",
                (_now(), worker_id),
            )
        return cursor.rowcount > 0

    def submit(self, request: ExecutionRequest) -> ExecutionResult:
        execution_id, now = str(uuid.uuid4()), _now()
        with self._connect() as db:
            task_row = db.execute("SELECT * FROM tasks WHERE task_ref=%s", (request.task_ref,)).fetchone()
            if not task_row:
                raise KeyError(f"unknown task_ref: {request.task_ref}")
            manifest = task_row["manifest_json"] if isinstance(task_row["manifest_json"], dict) else json.loads(task_row["manifest_json"])
            metadata = dict(request.metadata)
            metadata["task_digest"] = manifest.get("digest")
            metadata["evaluator"] = manifest.get("evaluator")
            metadata["runtime_image"] = manifest.get("runtime_image") or manifest.get("runtime")
            metadata["event_id"] = f"execution:{execution_id}:result"
            if request.timeout_seconds is not None:
                metadata["timeout_seconds"] = request.timeout_seconds
            inserted = db.execute(
                """INSERT INTO executions(execution_id,idempotency_key,task_ref,submission_path,callback_url,callback_token,
                   metadata_json,status,metrics_json,scores_json,artifacts_json,created_at,updated_at,queue,resource_class,priority)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(idempotency_key) DO NOTHING
                   RETURNING *""",
                (execution_id, request.idempotency_key, request.task_ref, request.submission_path, request.callback_url,
                 request.callback_token, json.dumps(metadata, sort_keys=True), "queued", json.dumps({}), json.dumps({}), json.dumps({}), now, now,
                 request.queue, request.resource_class, request.priority),
            ).fetchone()
            if inserted:
                return self._result(inserted)
            existing = db.execute("SELECT * FROM executions WHERE idempotency_key=%s", (request.idempotency_key,)).fetchone()
            if existing:
                return self._result(existing)
            raise RuntimeError("idempotent execution insert was lost before it could be read")

    def claim_next(self, *, worker_id: str = "local-worker", queues: tuple[str, ...] | None = None,
                   resource_classes: tuple[str, ...] | None = None, capabilities: tuple[str, ...] | None = None,
                   lease_seconds: int = 300):
        with self._connect() as db:
            now = datetime.now(UTC)
            db.execute("UPDATE executions SET status='queued',worker_id=NULL,lease_expires_at=NULL,updated_at=%s WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<=%s", (now, now))
            clauses, params = ["status='queued'", "cancel_requested=FALSE"], []
            if queues:
                clauses.append("queue = ANY(%s)")
                params.append(list(queues))
            if resource_classes:
                clauses.append("resource_class = ANY(%s)")
                params.append(list(resource_classes))
            rows = db.execute("SELECT e.*,t.manifest_json AS task_manifest_json FROM executions e JOIN tasks t ON t.task_ref=e.task_ref WHERE " + " AND ".join(clauses) + " ORDER BY e.priority DESC,e.created_at LIMIT 100 FOR UPDATE SKIP LOCKED", params).fetchall()
            available_capabilities = set(capabilities or ())
            row = None
            for candidate in rows:
                task_manifest = candidate["task_manifest_json"] if isinstance(candidate["task_manifest_json"], dict) else json.loads(candidate["task_manifest_json"])
                execution_metadata = candidate["metadata_json"] if isinstance(candidate["metadata_json"], dict) else json.loads(candidate["metadata_json"])
                required = set(task_manifest.get("required_capabilities") or ())
                required.update(execution_metadata.get("required_capabilities") or ())
                if required.issubset(available_capabilities):
                    row = candidate
                    break
            if not row:
                return None
            lease = now + timedelta(seconds=max(1, lease_seconds))
            db.execute("UPDATE executions SET status='running',worker_id=%s,lease_expires_at=%s,updated_at=%s WHERE execution_id=%s", (worker_id, lease, now, row["execution_id"]))
            task_row = db.execute("SELECT * FROM tasks WHERE task_ref=%s", (row["task_ref"],)).fetchone()
        if not task_row:
            return None
        return self.get_execution(row["execution_id"]), self._task(task_row), {"callback_url": row["callback_url"], "callback_token": row["callback_token"], "submission_path": row["submission_path"], "queue": row["queue"], "resource_class": row["resource_class"]}

    def finish(self, execution_id: str, result: ExecutionResult, *, worker_id: str | None = None) -> ExecutionResult | None:
        with self._connect() as db:
            # PostgreSQL cannot infer the type of a parameter used only in
            # ``%s IS NULL`` when the worker id is absent.  Explicitly cast
            # that guard to the column type so finishing an execution works
            # for both scoped and unscoped callers.
            cursor = db.execute("UPDATE executions SET status=%s,score=%s,metrics_json=%s,scores_json=%s,winner=%s,artifacts_json=%s,failure_reason=%s,metadata_json=%s,worker_id=NULL,lease_expires_at=NULL,updated_at=%s WHERE execution_id=%s AND (%s::text IS NULL OR worker_id=%s)", (result.status, result.score, json.dumps(result.metrics), json.dumps(result.scores), result.winner, json.dumps(result.artifacts), result.failure_reason, json.dumps(result.metadata), _now(), execution_id, worker_id, worker_id))
        if cursor.rowcount == 0:
            return None
        return self.get_execution(execution_id)

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

    def is_cancel_requested(self, execution_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT cancel_requested FROM executions WHERE execution_id=%s", (execution_id,)).fetchone()
        return bool(row and row["cancel_requested"])

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
        scores = row["scores_json"] if isinstance(row["scores_json"], dict) else json.loads(row["scores_json"] or "{}")
        artifacts = row["artifacts_json"] if isinstance(row["artifacts_json"], dict) else json.loads(row["artifacts_json"] or "{}")
        return ExecutionResult(row["execution_id"], row["task_ref"], row["status"], row["score"], metrics, row["failure_reason"], metadata, queue=row["queue"], resource_class=row["resource_class"], priority=row["priority"], task_digest=metadata.get("task_digest"), evaluator=metadata.get("evaluator"), runtime_image=metadata.get("runtime_image"), seed=metadata.get("seed"), event_id=metadata.get("event_id"), scores=scores, winner=row["winner"], artifacts=artifacts)

    @staticmethod
    def _worker(row: dict[str, Any]) -> WorkerRecord:
        def _list(value: Any) -> tuple[str, ...]:
            if isinstance(value, list):
                return tuple(str(item) for item in value)
            return tuple(json.loads(value))

        metadata = row["metadata_json"] if isinstance(row["metadata_json"], dict) else json.loads(row["metadata_json"])
        return WorkerRecord(
            worker_id=row["worker_id"],
            capabilities=_list(row["capabilities_json"]),
            queues=_list(row["queues_json"]),
            resource_classes=_list(row["resource_classes_json"]),
            region=row["region"],
            status=row["status"],
            draining=bool(row["draining"]),
            metadata=metadata,
        )
