"""Standalone FastAPI control plane.

FastAPI is optional so the core/CLI remain dependency-light. Install
``brunost-judge[server]`` for the HTTP service.
"""

import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from brunost_judge.artifacts import ArtifactError, ArtifactStore, pack_directory
from brunost_judge.contracts import TaskRecord
from brunost_judge.enrollment import digest_secret, expires_at, new_secret
from brunost_judge.store import create_store
from brunost_judge.task import task_digest, validate_task


def create_app(database: str | Path | None = None):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
    except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
        raise RuntimeError("Install brunost-judge[server] to run the HTTP API") from exc
    from pydantic import BaseModel, Field

    class TaskRequest(BaseModel):
        task_ref: str = Field(min_length=1, max_length=200)
        path: str | None = Field(default=None, min_length=1)
        artifact_id: str | None = Field(default=None, min_length=64, max_length=128)
        kind: str | None = Field(default=None, min_length=1, max_length=50)
        version: int = Field(default=1, ge=1)
        runtime: str = Field(default="python-3.13", min_length=1, max_length=100)
        evaluator: str | None = None
        resource_profile: dict[str, Any] = Field(default_factory=dict)
        required_capabilities: list[str] = Field(default_factory=list, max_length=32)
        metadata: dict[str, Any] = Field(default_factory=dict)

    class ExecutionRequestModel(BaseModel):
        task_ref: str
        submission_path: str | None = None
        submission_artifact_id: str | None = Field(default=None, min_length=64, max_length=128)
        idempotency_key: str = Field(min_length=1, max_length=255)
        callback_url: str | None = None
        callback_token: str | None = None
        metadata: dict[str, Any] = Field(default_factory=dict)
        queue: str = Field(default="default", min_length=1, max_length=100)
        resource_class: str = Field(default="cpu", min_length=1, max_length=50)
        priority: int = Field(default=0, ge=-100, le=100)
        evaluation_kind: str = Field(default="batch", min_length=1, max_length=50)
        agent_refs: list[str] = Field(default_factory=list, max_length=32)
        game_ref: str | None = None
        seed: int | None = None

    class AgentDefinitionModel(BaseModel):
        agent_id: str = Field(min_length=1, max_length=200)
        name: str = Field(min_length=1, max_length=200)
        version: str = Field(default="1", min_length=1, max_length=100)
        artifact_path: str | None = None
        protocol: str = Field(default="stdio", min_length=1, max_length=100)
        resource_profile: dict[str, Any] = Field(default_factory=dict)
        required_capabilities: list[str] = Field(default_factory=list, max_length=32)
        metadata: dict[str, Any] = Field(default_factory=dict)

    class GameDefinitionModel(BaseModel):
        game_id: str = Field(min_length=1, max_length=200)
        name: str = Field(min_length=1, max_length=200)
        task_ref: str = Field(min_length=1, max_length=200)
        seats: int = Field(default=2, ge=2, le=64)
        protocol: str = Field(default="stdio", min_length=1, max_length=100)
        referee: str | None = None
        resource_profile: dict[str, Any] = Field(default_factory=dict)
        required_capabilities: list[str] = Field(default_factory=list, max_length=32)
        metadata: dict[str, Any] = Field(default_factory=dict)

    class MatchRequestModel(BaseModel):
        agent_refs: list[str] = Field(min_length=2, max_length=64)
        submission_path: str = Field(min_length=1)
        idempotency_key: str = Field(min_length=1, max_length=255)
        seed: int | None = None
        callback_url: str | None = None
        callback_token: str | None = None
        metadata: dict[str, Any] = Field(default_factory=dict)
        queue: str = Field(default="default", min_length=1, max_length=100)
        resource_class: str = Field(default="cpu", min_length=1, max_length=50)
        priority: int = Field(default=0, ge=-100, le=100)

    class WorkerRegistrationModel(BaseModel):
        worker_id: str = Field(min_length=1, max_length=200)
        capabilities: list[str] = Field(default_factory=list, max_length=128)
        queues: list[str] = Field(default_factory=lambda: ["default"], max_length=32)
        resource_classes: list[str] = Field(default_factory=lambda: ["cpu"], max_length=32)
        region: str | None = None
        status: str = Field(default="ready", min_length=1, max_length=50)
        draining: bool = False
        metadata: dict[str, Any] = Field(default_factory=dict)

    class EnrollmentTokenRequestModel(BaseModel):
        node_id: str = Field(min_length=1, max_length=200)
        worker_id: str | None = Field(default=None, max_length=200)
        role: str = Field(default="worker", min_length=1, max_length=50)
        capabilities: list[str] = Field(default_factory=list, max_length=128)
        queues: list[str] = Field(default_factory=lambda: ["default"], max_length=32)
        resource_classes: list[str] = Field(default_factory=lambda: ["cpu"], max_length=32)
        region: str | None = Field(default=None, max_length=100)
        metadata: dict[str, Any] = Field(default_factory=dict)
        ttl_seconds: int = Field(default=900, ge=60, le=86400)

    class NodeEnrollmentModel(BaseModel):
        join_token: str = Field(min_length=20, max_length=500)
        hostname: str | None = Field(default=None, max_length=255)
        capabilities: list[str] = Field(default_factory=list, max_length=128)
        resource_classes: list[str] = Field(default_factory=list, max_length=32)
        metadata: dict[str, Any] = Field(default_factory=dict)

    class WorkerFinishModel(BaseModel):
        execution_id: str = Field(min_length=1, max_length=200)
        task_ref: str = Field(min_length=1, max_length=200)
        status: str = Field(min_length=1, max_length=50)
        score: float | None = None
        metrics: dict[str, Any] = Field(default_factory=dict)
        failure_reason: str | None = None
        metadata: dict[str, Any] = Field(default_factory=dict)
        judge_version: str = "local"
        queue: str = "default"
        resource_class: str = "cpu"
        priority: int = 0

    database_ref = database or os.environ.get("BRUNOST_JUDGE_DATABASE_URL") or os.environ.get("BRUNOST_JUDGE_DB", "judge.db")
    store = create_store(database_ref)
    artifact_store = ArtifactStore(
        os.environ.get("BRUNOST_JUDGE_ARTIFACT_ROOT", "artifacts"),
        max_bytes=int(os.environ.get("BRUNOST_JUDGE_ARTIFACT_MAX_BYTES", str(512 * 1024 * 1024))),
    )
    app = FastAPI(title="Brunost Judge", version="0.8.0")

    def _allowed_path(value: str, env_name: str) -> str:
        path = Path(value).expanduser().resolve()
        root = os.environ.get(env_name, "").strip()
        if root:
            allowed = Path(root).expanduser().resolve()
            if path != allowed and allowed not in path.parents:
                raise HTTPException(status_code=422, detail=f"path must be inside {env_name}")
        return str(path)

    def _artifact_path(identifier: str | None) -> str:
        if not identifier:
            raise HTTPException(status_code=422, detail="artifact_id is required")
        try:
            artifact_store.get(identifier)
        except (ArtifactError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=f"artifact not found: {identifier}") from exc
        return f"artifact://{identifier}"

    def _directory_artifact(value: str, env_name: str) -> tuple[str, str]:
        """Snapshot a directory immediately and return its immutable reference."""
        path = Path(_allowed_path(value, env_name))
        if not path.is_dir():
            raise HTTPException(status_code=422, detail=f"path is not a directory: {path}")
        try:
            stored = artifact_store.put(pack_directory(path))
        except (ArtifactError, OSError) as exc:
            raise HTTPException(status_code=422, detail=f"could not snapshot directory: {exc}") from exc
        return f"artifact://{stored['artifact_id']}", str(stored["artifact_id"])

    def _validate_callback_url(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=422, detail="callback_url must be an absolute http(s) URL")
        production = os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production"}
        require_https = production or os.environ.get("BRUNOST_JUDGE_REQUIRE_HTTPS_CALLBACKS", "false").lower() == "true"
        if require_https and parsed.scheme != "https":
            raise HTTPException(status_code=422, detail="callback_url must use https in production")
        allowlist = {host.strip().lower() for host in os.environ.get("BRUNOST_JUDGE_CALLBACK_HOSTS", "").split(",") if host.strip()}
        if production and not allowlist:
            raise HTTPException(status_code=503, detail="BRUNOST_JUDGE_CALLBACK_HOSTS is required in production")
        if allowlist and (not parsed.hostname or parsed.hostname.lower() not in allowlist):
            raise HTTPException(status_code=422, detail="callback host is not allowed")
        return url

    def require_api_token(authorization: str | None = Header(default=None)) -> None:
        expected = os.environ.get("BRUNOST_JUDGE_API_TOKEN", "").strip()
        required = os.environ.get("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "false").lower() == "true"
        if required and not expected:
            raise HTTPException(status_code=503, detail="judge API token is not configured")
        if expected and authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="invalid judge API token")

    def require_worker_token(worker_id: str, authorization: str | None = Header(default=None)) -> None:
        """Accept the global admin token or the enrolled worker's scoped token."""

        expected = os.environ.get("BRUNOST_JUDGE_API_TOKEN", "").strip()
        required = os.environ.get("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "false").lower() == "true"
        if expected and authorization == f"Bearer {expected}":
            return
        if authorization and authorization.startswith("Bearer "):
            if store.verify_worker_token(worker_id, authorization.removeprefix("Bearer ").strip()):
                return
            raise HTTPException(status_code=401, detail="invalid worker token")
        worker_required = os.environ.get("BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN", "false").lower() == "true"
        if expected or required or worker_required:
            raise HTTPException(status_code=401, detail="worker token required")

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "brunost-judge",
            "version": "0.8.0",
            "database": type(store).__name__,
            "cluster_id": os.environ.get("BRUNOST_JUDGE_CLUSTER_ID", "local"),
        }

    @app.get("/v1/cluster", dependencies=[Depends(require_api_token)])
    def cluster() -> dict[str, Any]:
        workers = store.list_workers()
        return {
            "cluster_id": os.environ.get("BRUNOST_JUDGE_CLUSTER_ID", "local"),
            "service": "brunost-judge",
            "version": "0.8.0",
            "workers": len(workers),
            "ready_workers": sum(1 for worker in workers if worker.status == "ready" and not worker.draining),
        }

    @app.put("/v1/artifacts/{artifact_id}", status_code=201, dependencies=[Depends(require_api_token)])
    async def upload_artifact(artifact_id: str, request: Request) -> dict[str, object]:
        try:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="invalid content-length") from exc
                if declared_size > artifact_store.max_bytes:
                    raise HTTPException(status_code=413, detail="artifact exceeds configured maximum size")
            body = await request.body()
            return artifact_store.put(body, expected_id=artifact_id)
        except HTTPException:
            raise
        except ArtifactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/workers/{worker_id}/artifacts/{artifact_id}", dependencies=[Depends(require_worker_token)])
    def download_artifact(worker_id: str, artifact_id: str) -> Response:
        try:
            data = artifact_store.get(artifact_id)
        except (ArtifactError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        return Response(content=data, media_type="application/gzip", headers={"X-Brunost-Artifact-SHA256": artifact_id})

    @app.post("/v1/nodes/enrollment-tokens", status_code=201, dependencies=[Depends(require_api_token)])
    def issue_enrollment_token(request: EnrollmentTokenRequestModel) -> dict[str, Any]:
        raw_token = new_secret()
        worker_id = request.worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        payload = {
            "node_id": request.node_id,
            "worker_id": worker_id,
            "role": request.role,
            "capabilities": request.capabilities,
            "queues": request.queues,
            "resource_classes": request.resource_classes,
            "region": request.region,
            "metadata": request.metadata,
        }
        record = store.create_enrollment_token(
            token_id=str(uuid.uuid4()),
            token_hash=digest_secret(raw_token),
            payload=payload,
            expires_at=expires_at(request.ttl_seconds),
        )
        return {**record, "join_token": raw_token}

    @app.post("/v1/nodes/enroll", status_code=201)
    def enroll_node(request: NodeEnrollmentModel) -> dict[str, Any]:
        payload = store.consume_enrollment_token(request.join_token)
        if payload is None:
            raise HTTPException(status_code=401, detail="invalid, expired, or already used join token")
        worker_id = str(payload["worker_id"])
        metadata = dict(payload.get("metadata") or {})
        metadata.update(request.metadata)
        if request.hostname:
            metadata["hostname"] = request.hostname
        if request.capabilities:
            metadata["detected_capabilities"] = sorted(set(request.capabilities))
        from brunost_judge.contracts import WorkerRecord

        capabilities = tuple(sorted(set(payload.get("capabilities") or ()) | set(request.capabilities)))
        resource_classes = tuple(sorted(set(payload.get("resource_classes") or ("cpu",)) | set(request.resource_classes)))
        worker = WorkerRecord(
            worker_id=worker_id,
            capabilities=capabilities,
            queues=tuple(payload.get("queues") or ("default",)),
            resource_classes=resource_classes,
            region=payload.get("region"),
            metadata={"node_id": payload.get("node_id"), "role": payload.get("role", "worker"), **metadata},
        )
        registered = store.register_worker(worker)
        worker_token = new_secret()
        store.create_worker_credential(worker_id, worker_token)
        return {
            "cluster_id": os.environ.get("BRUNOST_JUDGE_CLUSTER_ID", "local"),
            "node_id": payload.get("node_id"),
            "worker_token": worker_token,
            "worker": registered.as_dict(),
        }

    @app.get("/v1/tasks", dependencies=[Depends(require_api_token)])
    def list_tasks() -> list[dict[str, Any]]:
        return [task.as_dict() for task in store.list_tasks()]

    @app.post("/v1/tasks", status_code=201, dependencies=[Depends(require_api_token)])
    @app.post("/v1/task-definitions", status_code=201, dependencies=[Depends(require_api_token)])
    def register_task(request: TaskRequest) -> dict[str, Any]:
        if bool(request.path) == bool(request.artifact_id):
            raise HTTPException(status_code=422, detail="provide exactly one of path or artifact_id")
        digest = ""
        artifact_identifier: str | None = None
        if request.path:
            task_path = _allowed_path(request.path, "BRUNOST_TASK_ROOT")
            try:
                stored = artifact_store.put(pack_directory(task_path))
                artifact_identifier = str(stored["artifact_id"])
                materialized, temporary = artifact_store.materialize(artifact_identifier)
                validation = validate_task(materialized)
                if validation.valid:
                    digest = task_digest(validation.path)
                task_path = f"artifact://{artifact_identifier}"
            except (ArtifactError, OSError) as exc:
                raise HTTPException(status_code=422, detail=f"could not snapshot task: {exc}") from exc
            finally:
                if "temporary" in locals():
                    temporary.cleanup()
        else:
            task_path = _artifact_path(request.artifact_id)
            artifact_identifier = request.artifact_id
            try:
                materialized, temporary = artifact_store.materialize(request.artifact_id or "")
                validation = validate_task(materialized)
                digest = task_digest(validation.path) if validation.valid else ""
            finally:
                if "temporary" in locals():
                    temporary.cleanup()
        if not validation.valid:
            raise HTTPException(status_code=422, detail=list(validation.errors))
        manifest = {
            **request.metadata,
            "kind": request.kind or validation.kind,
            "version": request.version,
            "runtime": request.runtime,
            "evaluator": request.evaluator,
            "resource_profile": request.resource_profile,
            "required_capabilities": request.required_capabilities,
            "digest": digest,
        }
        if artifact_identifier:
            manifest["artifact_id"] = artifact_identifier
        task = store.register_task(TaskRecord(request.task_ref, task_path, request.kind or validation.kind or "unknown", manifest))
        return task.as_dict()

    @app.get("/v1/task-definitions", dependencies=[Depends(require_api_token)])
    def list_task_definitions() -> list[dict[str, Any]]:
        return [task.as_dict() for task in store.list_tasks()]

    def _submit_execution(payload: dict[str, Any]) -> dict[str, Any]:
        from brunost_judge.contracts import ExecutionRequest

        payload = dict(payload)
        submission_path = payload.pop("submission_path", None)
        submission_artifact_id = payload.pop("submission_artifact_id", None)
        if bool(submission_path) == bool(submission_artifact_id):
            raise HTTPException(status_code=422, detail="provide exactly one of submission_path or submission_artifact_id")
        if submission_path:
            payload["submission_path"], _ = _directory_artifact(submission_path, "BRUNOST_SUBMISSION_ROOT")
        else:
            payload["submission_path"] = _artifact_path(submission_artifact_id)
        payload["callback_url"] = _validate_callback_url(payload.get("callback_url"))
        metadata = dict(payload.pop("metadata", {}) or {})
        evaluation_kind = payload.pop("evaluation_kind", "batch")
        if evaluation_kind in {"agent", "match"}:
            raise HTTPException(status_code=501, detail=f"evaluation kind '{evaluation_kind}' requires an installed runner plugin")
        agent_refs = payload.pop("agent_refs", []) or []
        game_ref = payload.pop("game_ref", None)
        seed = payload.pop("seed", None)
        metadata["evaluation_kind"] = evaluation_kind
        if agent_refs:
            metadata["agent_refs"] = list(agent_refs)
        if game_ref:
            metadata["game_ref"] = game_ref
        if seed is not None:
            metadata["seed"] = seed
        payload["metadata"] = metadata
        try:
            result = store.submit(ExecutionRequest(**payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/executions", status_code=202, dependencies=[Depends(require_api_token)])
    def submit(request: ExecutionRequestModel) -> dict[str, Any]:
        return _submit_execution(request.model_dump())

    @app.post("/v1/evaluations", status_code=202, dependencies=[Depends(require_api_token)])
    def submit_evaluation(request: ExecutionRequestModel) -> dict[str, Any]:
        return _submit_execution(request.model_dump())

    @app.get("/v1/executions/{execution_id}", dependencies=[Depends(require_api_token)])
    def get_execution(execution_id: str) -> dict[str, Any]:
        result = store.get_execution(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return result.as_dict()

    @app.get("/v1/evaluations/{evaluation_id}", dependencies=[Depends(require_api_token)])
    def get_evaluation(evaluation_id: str) -> dict[str, Any]:
        return get_execution(evaluation_id)

    @app.get("/v1/executions", dependencies=[Depends(require_api_token)])
    def list_executions(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [result.as_dict() for result in store.list_executions(status=status, limit=limit)]

    @app.get("/v1/stats", dependencies=[Depends(require_api_token)])
    def stats() -> dict[str, int]:
        return store.stats()

    @app.post("/v1/executions/{execution_id}/cancel", dependencies=[Depends(require_api_token)])
    def cancel(execution_id: str) -> dict[str, Any]:
        result = store.cancel(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return result.as_dict()

    @app.post("/v1/agents", status_code=201, dependencies=[Depends(require_api_token)])
    def register_agent(request: AgentDefinitionModel) -> dict[str, Any]:
        payload = request.model_dump()
        artifact_path = payload.get("artifact_path")
        if artifact_path:
            payload["artifact_path"] = _allowed_path(artifact_path, "BRUNOST_AGENT_ROOT")
        return store.register_definition("agent", request.agent_id, payload)

    @app.get("/v1/agents", dependencies=[Depends(require_api_token)])
    def list_agents() -> list[dict[str, Any]]:
        return store.list_definitions("agent")

    @app.get("/v1/agents/{agent_id}", dependencies=[Depends(require_api_token)])
    def get_agent(agent_id: str) -> dict[str, Any]:
        value = store.get_definition("agent", agent_id)
        if value is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return value

    @app.post("/v1/games", status_code=201, dependencies=[Depends(require_api_token)])
    def register_game(request: GameDefinitionModel) -> dict[str, Any]:
        if store.get_task(request.task_ref) is None:
            raise HTTPException(status_code=404, detail=f"unknown task_ref: {request.task_ref}")
        return store.register_definition("game", request.game_id, request.model_dump())

    @app.get("/v1/games", dependencies=[Depends(require_api_token)])
    def list_games() -> list[dict[str, Any]]:
        return store.list_definitions("game")

    @app.get("/v1/games/{game_id}", dependencies=[Depends(require_api_token)])
    def get_game(game_id: str) -> dict[str, Any]:
        value = store.get_definition("game", game_id)
        if value is None:
            raise HTTPException(status_code=404, detail="game not found")
        return value

    @app.post("/v1/games/{game_id}/matches", status_code=202, dependencies=[Depends(require_api_token)])
    def submit_match(game_id: str, request: MatchRequestModel) -> dict[str, Any]:
        game = store.get_definition("game", game_id)
        if game is None:
            raise HTTPException(status_code=404, detail="game not found")
        if len(request.agent_refs) != int(game.get("seats", 2)):
            raise HTTPException(status_code=422, detail="agent_refs count must match game seats")
        payload = request.model_dump()
        payload.update({"task_ref": game["task_ref"], "evaluation_kind": "match", "game_ref": game_id})
        payload["agent_refs"] = request.agent_refs
        return _submit_execution(payload)

    @app.get("/v1/workers/capabilities", dependencies=[Depends(require_api_token)])
    def worker_capabilities() -> dict[str, Any]:
        workers = store.list_workers()
        if workers:
            capabilities = sorted({capability for worker in workers for capability in worker.capabilities})
            return {
                "worker_id": os.environ.get("BRUNOST_JUDGE_WORKER_ID", "control-plane"),
                "capabilities": capabilities,
                "workers": [worker.as_dict() for worker in workers],
            }
        raw = os.environ.get("BRUNOST_JUDGE_CAPABILITIES", "runtime:local,resource:cpu")
        capabilities = sorted({item.strip() for item in raw.split(",") if item.strip()})
        return {"worker_id": os.environ.get("BRUNOST_JUDGE_WORKER_ID", "control-plane"), "capabilities": capabilities}

    @app.post("/v1/workers/register", status_code=201, dependencies=[Depends(require_api_token)])
    def register_worker(request: WorkerRegistrationModel) -> dict[str, Any]:
        from brunost_judge.contracts import WorkerRecord

        worker = WorkerRecord(
            worker_id=request.worker_id,
            capabilities=tuple(request.capabilities),
            queues=tuple(request.queues),
            resource_classes=tuple(request.resource_classes),
            region=request.region,
            status=request.status,
            draining=request.draining,
            metadata=request.metadata,
        )
        return store.register_worker(worker).as_dict()

    @app.get("/v1/workers", dependencies=[Depends(require_api_token)])
    def list_workers() -> list[dict[str, Any]]:
        return [worker.as_dict() for worker in store.list_workers()]

    @app.post("/v1/workers/{worker_id}/heartbeat", dependencies=[Depends(require_worker_token)])
    def heartbeat_worker(worker_id: str, status: str = "ready") -> dict[str, Any]:
        worker = store.heartbeat_worker(worker_id, status=status)
        if worker is None:
            raise HTTPException(status_code=404, detail="worker not found")
        return worker.as_dict()

    @app.get("/v1/workers/{worker_id}/status", dependencies=[Depends(require_worker_token)])
    def worker_status(worker_id: str) -> dict[str, Any]:
        worker = store.get_worker(worker_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="worker not found")
        return worker.as_dict()

    @app.post("/v1/workers/{worker_id}/claim", response_model=None, dependencies=[Depends(require_worker_token)])
    def claim_worker(worker_id: str) -> dict[str, Any] | Response:
        worker = store.get_worker(worker_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="worker not found")
        if worker.draining or worker.status == "offline":
            return Response(status_code=204)
        claimed = store.claim_next(
            worker_id=worker_id,
            queues=worker.queues,
            resource_classes=worker.resource_classes,
            lease_seconds=int(os.environ.get("BRUNOST_JUDGE_LEASE_SECONDS", "300")),
        )
        if claimed is None:
            return Response(status_code=204)
        execution, task, context = claimed
        return {"execution": execution.as_dict(), "task": task.as_dict(), "context": context}

    @app.post("/v1/workers/{worker_id}/finish", dependencies=[Depends(require_worker_token)])
    def finish_worker(worker_id: str, request: WorkerFinishModel) -> dict[str, Any]:
        from brunost_judge.contracts import ExecutionResult

        result = store.finish(
            request.execution_id,
            ExecutionResult(
                execution_id=request.execution_id,
                task_ref=request.task_ref,
                status=request.status,
                score=request.score,
                metrics=request.metrics,
                failure_reason=request.failure_reason,
                metadata=request.metadata,
                judge_version=request.judge_version,
                queue=request.queue,
                resource_class=request.resource_class,
                priority=request.priority,
            ),
            worker_id=worker_id,
        )
        if result is None:
            raise HTTPException(status_code=409, detail="execution is not leased to this worker")
        return result.as_dict()

    @app.post("/v1/workers/{worker_id}/drain", dependencies=[Depends(require_api_token)])
    def drain_worker(worker_id: str, draining: bool = True) -> dict[str, Any]:
        worker = store.drain_worker(worker_id, draining=draining)
        if worker is None:
            raise HTTPException(status_code=404, detail="worker not found")
        return worker.as_dict()

    @app.post("/v1/workers/{worker_id}/credential/revoke", dependencies=[Depends(require_api_token)])
    def revoke_worker_credential(worker_id: str) -> dict[str, Any]:
        if not store.revoke_worker_credential(worker_id):
            raise HTTPException(status_code=404, detail="worker credential not found")
        return {"worker_id": worker_id, "revoked": True}

    @app.get("/console", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
    def console() -> str:
        return """<!doctype html><html><head><meta charset='utf-8'><title>Brunost Judge</title>
        <style>body{font:16px system-ui;max-width:980px;margin:2rem auto;padding:0 1rem}section{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}input{padding:.5rem;margin:.25rem;width:32rem}button{padding:.5rem 1rem}table{width:100%;border-collapse:collapse}td,th{padding:.4rem;border-bottom:1px solid #eee;text-align:left}.muted{color:#666}</style></head>
        <body><h1>Brunost Judge</h1><p class='muted'>Standalone operator console · <a href='/docs'>API documentation</a></p>
        <section><h2>Operator access</h2><input id='api-token' type='password' placeholder='API token (stored only in this browser)' onchange='localStorage.setItem("brunost-token",this.value)'></section>
        <section><h2>Add worker node</h2><input id='node-id' placeholder='node id, e.g. country-node-2'><input id='node-worker-id' placeholder='worker id (optional)'><input id='node-region' placeholder='region (optional)'><button onclick='issueNodeToken()'>Create one-time join token</button><pre id='node-message'></pre></section>
        <section><h2>Register task</h2><input id='task-ref' placeholder='task reference, e.g. demo/v1'><input id='task-path' placeholder='task path visible to the API'><button onclick='registerTask()'>Register</button><pre id='task-message'></pre></section>
        <section><h2>Submit execution</h2><input id='exec-task' placeholder='task reference'><input id='submission-path' placeholder='submission directory path'><input id='idempotency' placeholder='idempotency key'><button onclick='submitExecution()'>Submit</button><pre id='exec-message'></pre></section>
        <section><h2>Cluster</h2><pre id='cluster'>Loading…</pre><table><thead><tr><th>Worker</th><th>Region</th><th>Resources</th><th>Capabilities</th><th>Status</th></tr></thead><tbody id='workers'></tbody></table></section>
        <section><h2>Queue</h2><pre id='stats'>Loading…</pre><button onclick='refresh()'>Refresh</button><table><thead><tr><th>ID</th><th>Task</th><th>Status</th><th>Score</th></tr></thead><tbody id='executions'></tbody></table></section>
        <section><h2>Registered tasks</h2><table><thead><tr><th>Reference</th><th>Kind</th><th>Path</th></tr></thead><tbody id='tasks'></tbody></table></section>
        <script>
        const token=localStorage.getItem('brunost-token')||'';document.querySelector('#api-token').value=token;
        async function api(path, options){const headers={'Content-Type':'application/json'};const current=localStorage.getItem('brunost-token')||'';if(current)headers.Authorization='Bearer '+current;const r=await fetch(path,{headers,...options});const d=await r.json();if(!r.ok)throw new Error(JSON.stringify(d));return d}
        async function registerTask(){try{const d=await api('/v1/tasks',{method:'POST',body:JSON.stringify({task_ref:document.querySelector('#task-ref').value,path:document.querySelector('#task-path').value})});document.querySelector('#task-message').textContent=JSON.stringify(d,null,2);refresh()}catch(e){document.querySelector('#task-message').textContent=e}}
        async function issueNodeToken(){try{const node=document.querySelector('#node-id').value;const d=await api('/v1/nodes/enrollment-tokens',{method:'POST',body:JSON.stringify({node_id:node,worker_id:document.querySelector('#node-worker-id').value||null,region:document.querySelector('#node-region').value||null})});document.querySelector('#node-message').textContent='Copy this one-time token to the node:\n'+d.join_token+'\n\nRun: brunost node join --url '+location.origin+' --join-token <token>'}catch(e){document.querySelector('#node-message').textContent=e}}
        async function submitExecution(){try{const d=await api('/v1/executions',{method:'POST',body:JSON.stringify({task_ref:document.querySelector('#exec-task').value,submission_path:document.querySelector('#submission-path').value,idempotency_key:document.querySelector('#idempotency').value})});document.querySelector('#exec-message').textContent=JSON.stringify(d,null,2)}catch(e){document.querySelector('#exec-message').textContent=e}}
        async function refresh(){try{const [rows,executions,stats,cluster,workers]=await Promise.all([api('/v1/tasks'),api('/v1/executions?limit=50'),api('/v1/stats'),api('/v1/cluster'),api('/v1/workers')]);document.querySelector('#tasks').innerHTML=rows.map(t=>`<tr><td>${t.task_ref}</td><td>${t.kind}</td><td>${t.path}</td></tr>`).join('');document.querySelector('#executions').innerHTML=executions.map(e=>`<tr><td>${e.execution_id.slice(0,8)}</td><td>${e.task_ref}</td><td>${e.status}</td><td>${e.score??'—'}</td></tr>`).join('');document.querySelector('#workers').innerHTML=workers.map(w=>`<tr><td>${w.worker_id}</td><td>${w.region??'—'}</td><td>${w.resource_classes.join(', ')}</td><td>${w.capabilities.join(', ')}</td><td>${w.draining?'draining':w.status}</td></tr>`).join('');document.querySelector('#cluster').textContent=JSON.stringify(cluster,null,2);document.querySelector('#stats').textContent=JSON.stringify(stats,null,2)}catch(e){document.querySelector('#tasks').innerHTML='<tr><td colspan=3>'+e+'</td></tr>'}}
        refresh();
        </script></body></html>"""

    return app


app = create_app() if os.environ.get("BRUNOST_JUDGE_IMPORT_APP", "false").lower() == "true" else None
