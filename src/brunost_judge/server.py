"""Standalone FastAPI control plane.

FastAPI is optional so the core/CLI remain dependency-light. Install
``brunost-judge[server]`` for the HTTP service.
"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from brunost_judge.contracts import TaskRecord
from brunost_judge.store import create_store
from brunost_judge.task import task_digest, validate_task


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
        queue: str = Field(default="default", min_length=1, max_length=100)
        resource_class: str = Field(default="cpu", min_length=1, max_length=50)
        priority: int = Field(default=0, ge=-100, le=100)

    database_ref = database or os.environ.get("BRUNOST_JUDGE_DATABASE_URL") or os.environ.get("BRUNOST_JUDGE_DB", "judge.db")
    store = create_store(database_ref)
    app = FastAPI(title="Brunost Judge", version="0.3.0")

    def _allowed_path(value: str, env_name: str) -> str:
        path = Path(value).expanduser().resolve()
        root = os.environ.get(env_name, "").strip()
        if root:
            allowed = Path(root).expanduser().resolve()
            if path != allowed and allowed not in path.parents:
                raise HTTPException(status_code=422, detail=f"path must be inside {env_name}")
        return str(path)

    def _validate_callback_url(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=422, detail="callback_url must be an absolute http(s) URL")
        allowlist = {host.strip().lower() for host in os.environ.get("BRUNOST_JUDGE_CALLBACK_HOSTS", "").split(",") if host.strip()}
        if allowlist and parsed.hostname and parsed.hostname.lower() not in allowlist:
            raise HTTPException(status_code=422, detail="callback host is not allowed")
        return url

    def require_api_token(authorization: str | None = Header(default=None)) -> None:
        expected = os.environ.get("BRUNOST_JUDGE_API_TOKEN", "").strip()
        required = os.environ.get("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "false").lower() == "true"
        if required and not expected:
            raise HTTPException(status_code=503, detail="judge API token is not configured")
        if expected and authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="invalid judge API token")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "brunost-judge", "database": type(store).__name__}

    @app.get("/v1/tasks", dependencies=[Depends(require_api_token)])
    def list_tasks() -> list[dict[str, Any]]:
        return [task.as_dict() for task in store.list_tasks()]

    @app.post("/v1/tasks", status_code=201, dependencies=[Depends(require_api_token)])
    def register_task(request: TaskRequest) -> dict[str, Any]:
        task_path = _allowed_path(request.path, "BRUNOST_TASK_ROOT")
        validation = validate_task(task_path)
        if not validation.valid:
            raise HTTPException(status_code=422, detail=list(validation.errors))
        manifest = {"kind": validation.kind, "version": 1, "digest": task_digest(validation.path)}
        task = store.register_task(TaskRecord(request.task_ref, str(validation.path), validation.kind or "unknown", manifest))
        return task.as_dict()

    @app.post("/v1/executions", status_code=202, dependencies=[Depends(require_api_token)])
    def submit(request: ExecutionRequestModel) -> dict[str, Any]:
        from brunost_judge.contracts import ExecutionRequest

        payload = request.model_dump()
        payload["submission_path"] = _allowed_path(payload["submission_path"], "BRUNOST_SUBMISSION_ROOT")
        payload["callback_url"] = _validate_callback_url(payload.get("callback_url"))
        try:
            result = store.submit(ExecutionRequest(**payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result.as_dict()

    @app.get("/v1/executions/{execution_id}", dependencies=[Depends(require_api_token)])
    def get_execution(execution_id: str) -> dict[str, Any]:
        result = store.get_execution(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return result.as_dict()

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

    @app.get("/console", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
    def console() -> str:
        return """<!doctype html><html><head><meta charset='utf-8'><title>Brunost Judge</title>
        <style>body{font:16px system-ui;max-width:980px;margin:2rem auto;padding:0 1rem}section{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}input{padding:.5rem;margin:.25rem;width:32rem}button{padding:.5rem 1rem}table{width:100%;border-collapse:collapse}td,th{padding:.4rem;border-bottom:1px solid #eee;text-align:left}.muted{color:#666}</style></head>
        <body><h1>Brunost Judge</h1><p class='muted'>Standalone operator console · <a href='/docs'>API documentation</a></p>
        <section><h2>Operator access</h2><input id='api-token' type='password' placeholder='API token (stored only in this browser)' onchange='localStorage.setItem("brunost-token",this.value)'></section>
        <section><h2>Register task</h2><input id='task-ref' placeholder='task reference, e.g. demo/v1'><input id='task-path' placeholder='task path visible to the API'><button onclick='registerTask()'>Register</button><pre id='task-message'></pre></section>
        <section><h2>Submit execution</h2><input id='exec-task' placeholder='task reference'><input id='submission-path' placeholder='submission directory path'><input id='idempotency' placeholder='idempotency key'><button onclick='submitExecution()'>Submit</button><pre id='exec-message'></pre></section>
        <section><h2>Queue</h2><pre id='stats'>Loading…</pre><button onclick='refresh()'>Refresh</button><table><thead><tr><th>ID</th><th>Task</th><th>Status</th><th>Score</th></tr></thead><tbody id='executions'></tbody></table></section>
        <section><h2>Registered tasks</h2><table><thead><tr><th>Reference</th><th>Kind</th><th>Path</th></tr></thead><tbody id='tasks'></tbody></table></section>
        <script>
        const token=localStorage.getItem('brunost-token')||'';document.querySelector('#api-token').value=token;
        async function api(path, options){const headers={'Content-Type':'application/json'};const current=localStorage.getItem('brunost-token')||'';if(current)headers.Authorization='Bearer '+current;const r=await fetch(path,{headers,...options});const d=await r.json();if(!r.ok)throw new Error(JSON.stringify(d));return d}
        async function registerTask(){try{const d=await api('/v1/tasks',{method:'POST',body:JSON.stringify({task_ref:document.querySelector('#task-ref').value,path:document.querySelector('#task-path').value})});document.querySelector('#task-message').textContent=JSON.stringify(d,null,2);refresh()}catch(e){document.querySelector('#task-message').textContent=e}}
        async function submitExecution(){try{const d=await api('/v1/executions',{method:'POST',body:JSON.stringify({task_ref:document.querySelector('#exec-task').value,submission_path:document.querySelector('#submission-path').value,idempotency_key:document.querySelector('#idempotency').value})});document.querySelector('#exec-message').textContent=JSON.stringify(d,null,2)}catch(e){document.querySelector('#exec-message').textContent=e}}
        async function refresh(){try{const [rows,executions,stats]=await Promise.all([api('/v1/tasks'),api('/v1/executions?limit=50'),api('/v1/stats')]);document.querySelector('#tasks').innerHTML=rows.map(t=>`<tr><td>${t.task_ref}</td><td>${t.kind}</td><td>${t.path}</td></tr>`).join('');document.querySelector('#executions').innerHTML=executions.map(e=>`<tr><td>${e.execution_id.slice(0,8)}</td><td>${e.task_ref}</td><td>${e.status}</td><td>${e.score??'—'}</td></tr>`).join('');document.querySelector('#stats').textContent=JSON.stringify(stats,null,2)}catch(e){document.querySelector('#tasks').innerHTML='<tr><td colspan=3>'+e+'</td></tr>'}}
        refresh();
        </script></body></html>"""

    return app


app = create_app() if os.environ.get("BRUNOST_JUDGE_IMPORT_APP", "false").lower() == "true" else None
