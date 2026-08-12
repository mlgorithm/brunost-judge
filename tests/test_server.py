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
    response = client.post("/v1/tasks", json={"task_ref": "demo/v1", "path": str(task)})
    assert response.status_code == 201
    response = client.post("/v1/executions", json={"task_ref": "demo/v1", "submission_path": str(submission), "idempotency_key": "one"})
    assert response.status_code == 202
    execution_id = response.json()["execution_id"]
    assert client.get(f"/v1/executions/{execution_id}").json()["status"] == "queued"


def test_api_token_protects_control_plane(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "secret")
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.get("/v1/tasks").status_code == 401
    assert client.get("/v1/tasks", headers={"Authorization": "Bearer secret"}).status_code == 200
