from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.server import create_app


def test_api_registers_and_submits(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "public").mkdir()
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()

    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.get("/readyz").json()["status"] == "ready"
    response = client.post("/v1/tasks", json={"task_ref": "demo/v1", "path": str(task)})
    assert response.status_code == 201
    response = client.post("/v1/executions", json={"task_ref": "demo/v1", "submission_path": str(submission), "idempotency_key": "one"})
    assert response.status_code == 202
    execution_id = response.json()["execution_id"]
    assert client.get(f"/v1/executions/{execution_id}").json()["status"] == "queued"
    assert client.get("/v1/stats").json()["queued"] == 1
    assert client.get("/v1/executions").json()[0]["queue"] == "default"


def test_task_kind_override_must_match_manifest(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "public").mkdir()
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    response = TestClient(create_app(tmp_path / "judge.db")).post(
        "/v1/tasks", json={"task_ref": "mismatch/v1", "path": str(task), "kind": "icpc"}
    )
    assert response.status_code == 422
    assert "match" in response.json()["detail"]


def test_api_token_protects_control_plane(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "secret")
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.get("/v1/tasks").status_code == 401
    assert client.get("/v1/tasks", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_api_is_closed_without_explicit_auth_configuration(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BRUNOST_JUDGE_API_TOKEN", raising=False)
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "false")
    monkeypatch.delenv("BRUNOST_JUDGE_ALLOW_ANONYMOUS_API", raising=False)
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.get("/v1/tasks").status_code == 503


def test_operator_console_is_available(tmp_path: Path):
    response = TestClient(create_app(tmp_path / "judge.db")).get("/console")
    assert response.status_code == 200
    assert "Register task" in response.text
    assert "function escapeHtml" in response.text
    assert "${escapeHtml(t.task_ref)}" in response.text


def test_api_rejects_evaluation_modes_that_do_not_match_the_task(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()
    client = TestClient(create_app(tmp_path / "judge.db"))

    assert client.post("/v1/tasks", json={"task_ref": "ioai/v1", "path": str(task)}).status_code == 201
    assert client.post(
        "/v1/games",
        json={"game_id": "wrong-game", "name": "Wrong game", "task_ref": "ioai/v1"},
    ).status_code == 422
    agent_response = client.post(
        "/v1/evaluations",
        json={
            "task_ref": "ioai/v1",
            "submission_path": str(submission),
            "idempotency_key": "wrong-agent-mode",
            "evaluation_kind": "agent",
            "agent_refs": ["unused"],
        },
    )
    assert agent_response.status_code == 422
    assert "requires an agent task" in agent_response.json()["detail"]
    interactive_response = client.post(
        "/v1/evaluations",
        json={
            "task_ref": "ioai/v1",
            "submission_path": str(submission),
            "idempotency_key": "wrong-interactive-mode",
            "evaluation_kind": "interactive",
        },
    )
    assert interactive_response.status_code == 422


def test_api_rejects_reusing_an_idempotency_key_for_a_different_request(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / "submission.py").write_text("print('different')\n", encoding="utf-8")
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.post("/v1/tasks", json={"task_ref": "ioai/v1", "path": str(task)}).status_code == 201
    assert client.post(
        "/v1/evaluations",
        json={"task_ref": "ioai/v1", "submission_path": str(first), "idempotency_key": "same-key"},
    ).status_code == 202
    conflict = client.post(
        "/v1/evaluations",
        json={"task_ref": "ioai/v1", "submission_path": str(second), "idempotency_key": "same-key"},
    )
    assert conflict.status_code == 409
    assert "different request" in conflict.json()["detail"]


def test_production_allows_only_explicit_allowlisted_internal_http_callback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ENV", "production")
    monkeypatch.setenv("BRUNOST_JUDGE_CALLBACK_HOSTS", "premium")
    monkeypatch.setenv("BRUNOST_JUDGE_ALLOW_INTERNAL_HTTP_CALLBACKS", "true")
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "public").mkdir()
    (task / "private").mkdir()
    (task / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.post("/v1/tasks", json={"task_ref": "internal/v1", "path": str(task)}).status_code == 201
    allowed = client.post(
        "/v1/executions",
        json={
            "task_ref": "internal/v1",
            "submission_path": str(submission),
            "idempotency_key": "internal-http",
            "callback_url": "http://premium:3100/api/judge/callback",
        },
    )
    assert allowed.status_code == 202
    rejected = client.post(
        "/v1/executions",
        json={
            "task_ref": "internal/v1",
            "submission_path": str(submission),
            "idempotency_key": "unlisted-http",
            "callback_url": "http://unlisted:3000/api/judge/callback",
        },
    )
    assert rejected.status_code == 422
