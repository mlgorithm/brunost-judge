from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.auth import configured_secret, write_secret_file
from brunost_judge.server import create_app


def test_secret_file_loading_and_atomic_permissions(tmp_path: Path, monkeypatch):
    secret_file = tmp_path / "secrets" / "admin-token"
    write_secret_file(secret_file, "file-secret")
    monkeypatch.delenv("BRUNOST_JUDGE_API_TOKEN", raising=False)
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN_FILE", str(secret_file))
    assert configured_secret("BRUNOST_JUDGE_API_TOKEN") == "file-secret"
    assert oct(secret_file.stat().st_mode & 0o777) == "0o600"


def test_admin_token_rotation_reloads_file_without_restart(tmp_path: Path, monkeypatch):
    secret_file = tmp_path / "admin-token"
    write_secret_file(secret_file, "old-secret")
    monkeypatch.delenv("BRUNOST_JUDGE_API_TOKEN", raising=False)
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN_FILE", str(secret_file))
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    client = TestClient(create_app(tmp_path / "judge.db"))
    old_headers = {"Authorization": "Bearer old-secret"}

    rotated = client.post("/v1/auth/admin-token/rotate", headers=old_headers)
    assert rotated.status_code == 200
    new_token = rotated.json()["token"]
    assert client.get("/v1/stats", headers=old_headers).status_code == 401
    assert client.get("/v1/stats", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200
    assert secret_file.read_text(encoding="utf-8").strip() == new_token


def test_service_credentials_are_scoped_hashed_and_revocable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}
    created = client.post(
        "/v1/auth/service-credentials",
        headers=admin,
        json={"name": "premium-api", "scopes": ["judge:read", "judge:write"]},
    )
    assert created.status_code == 201
    payload = created.json()
    service = {"Authorization": f"Bearer {payload['token']}"}
    assert client.get("/v1/stats", headers=service).status_code == 200
    assert client.post("/v1/nodes/enrollment-tokens", headers=service, json={"node_id": "no-elevation"}).status_code == 401
    assert client.post(f"/v1/auth/service-credentials/{payload['credential_id']}/revoke", headers=admin).status_code == 200
    assert client.get("/v1/stats", headers=service).status_code == 401


def test_audit_events_and_rate_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    monkeypatch.setenv("BRUNOST_JUDGE_RATE_LIMIT_PER_MINUTE", "1")
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}
    assert client.get("/v1/stats", headers=admin).status_code == 200
    limited = client.get("/v1/stats", headers=admin)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"]

    monkeypatch.setenv("BRUNOST_JUDGE_RATE_LIMIT_PER_MINUTE", "300")
    assert client.post(
        "/v1/auth/service-credentials",
        headers=admin,
        json={"name": "audit-check", "scopes": ["judge:read"]},
    ).status_code == 201
    events = client.get("/v1/audit", headers=admin)
    assert events.status_code == 200
    assert any(event["action"] == "POST /v1/auth/service-credentials" and event["actor"] == "admin" for event in events.json())


def test_signed_callback_requirement_fails_closed(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS", "true")
    monkeypatch.delenv("BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET_FILE", raising=False)
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.post("/v1/tasks", json={"task_ref": "signed/v1", "path": str(task)}).status_code == 201
    response = client.post(
        "/v1/executions",
        json={
            "task_ref": "signed/v1",
            "submission_path": str(submission),
            "idempotency_key": "signed-callback",
            "callback_url": "https://platform.example/callback",
        },
    )
    assert response.status_code == 503
