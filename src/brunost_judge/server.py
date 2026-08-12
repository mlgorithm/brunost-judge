"""Standalone FastAPI control plane.

FastAPI is optional so the core/CLI remain dependency-light. Install
``brunost-judge[server]`` for the HTTP service.
"""

import os
from pathlib import Path
from typing import Any

from brunost_judge.contracts import TaskRecord
from brunost_judge.store import JudgeStore
from brunost_judge.task import validate_task


def create_app(database: str | Path | None = None):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
    except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
        raise RuntimeError("Install brunost-judge[server] to run the HTTP API") from exc
    from pydantic import BaseModel, Field

    class TaskRequest(BaseModel):
        task_ref: str = Field(min_length=1, max_length=200)
        path: str = Field(min_length=1)

    class ExecutionRequestModel(BaseModel):
        task_ref: str
        submission_path: str
        idempotency_key: str = Field(min_length=1, max_length=255)
        callback_url: str | None = None
        callback_token: str | None = None
        metadata: dict[str, Any] = Field(default_factory=dict)

    store = JudgeStore(database or os.environ.get("BRUNOST_JUDGE_DB", "judge.db"))
    app = FastAPI(title="Brunost Judge", version="0.1.0")

    def require_api_token(authorization: str | None = Header(default=None)) -> None:
        expected = os.environ.get("BRUNOST_JUDGE_API_TOKEN", "").strip()
        if expected and authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="invalid judge API token")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "brunost-judge"}

    @app.get("/v1/tasks", dependencies=[Depends(require_api_token)])
    def list_tasks() -> list[dict[str, Any]]:
        return [task.as_dict() for task in store.list_tasks()]

    @app.post("/v1/tasks", status_code=201, dependencies=[Depends(require_api_token)])
    def register_task(request: TaskRequest) -> dict[str, Any]:
        validation = validate_task(request.path)
        if not validation.valid:
            raise HTTPException(status_code=422, detail=list(validation.errors))
        manifest = {"kind": validation.kind}
        task = store.register_task(TaskRecord(request.task_ref, str(validation.path), validation.kind or "unknown", manifest))
        return task.as_dict()

    @app.post("/v1/executions", status_code=202, dependencies=[Depends(require_api_token)])
    def submit(request: ExecutionRequestModel) -> dict[str, Any]:
        from brunost_judge.contracts import ExecutionRequest

        try:
            result = store.submit(ExecutionRequest(**request.model_dump()))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result.as_dict()

    @app.get("/v1/executions/{execution_id}", dependencies=[Depends(require_api_token)])
    def get_execution(execution_id: str) -> dict[str, Any]:
        result = store.get_execution(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return result.as_dict()

    @app.post("/v1/executions/{execution_id}/cancel", dependencies=[Depends(require_api_token)])
    def cancel(execution_id: str) -> dict[str, Any]:
        result = store.cancel(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return result.as_dict()

    @app.get("/console", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
    def console() -> str:
        return """<!doctype html><html><head><title>Brunost Judge</title></head><body><h1>Brunost Judge</h1><p>API is healthy. Use <a href='/docs'>API docs</a> or the SDK to register tasks and submit executions.</p></body></html>"""

    return app


app = create_app() if os.environ.get("BRUNOST_JUDGE_IMPORT_APP", "false").lower() == "true" else None
