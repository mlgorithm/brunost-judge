from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.contracts import EvaluationRequest, ResourceProfile
from brunost_judge.server import create_app


def _task(root: Path) -> Path:
    task = root / "task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: agent\n", encoding="utf-8")
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return {'public': 1.0}\n", encoding="utf-8")
    return task


def test_public_contracts_are_serializable():
    profile = ResourceProfile(cpu_cores=2, memory_mb=1024)
    request = EvaluationRequest(
        task_ref="demo/v1",
        submission_path="/tmp/submission",
        idempotency_key="one",
        evaluation_kind="agent",
        agent_refs=("baseline",),
        seed=42,
    )
    assert profile.as_dict()["memory_mb"] == 1024
    assert request.as_dict()["agent_refs"] == ["baseline"]
    assert request.to_execution_request().metadata["seed"] == 42


def test_agent_game_and_match_api(tmp_path: Path):
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    client = TestClient(create_app(tmp_path / "judge.db"))

    assert client.post("/v1/tasks", json={"task_ref": "game/v1", "path": str(task)}).status_code == 201
    agent = client.post("/v1/agents", json={"agent_id": "baseline", "name": "Baseline"})
    assert agent.status_code == 201
    assert client.get("/v1/agents/baseline").json()["name"] == "Baseline"
    game = client.post("/v1/games", json={"game_id": "connect4", "name": "Connect Four", "task_ref": "game/v1", "seats": 2})
    assert game.status_code == 201
    match = client.post(
        "/v1/games/connect4/matches",
        json={
            "agent_refs": ["baseline", "baseline"],
            "submission_path": str(submission),
            "idempotency_key": "match-1",
            "seed": 7,
        },
    )
    assert match.status_code == 202
    assert match.json()["evaluation_id"] == match.json()["execution_id"]
    assert match.json()["metadata"]["evaluation_kind"] == "match"


def test_evaluation_alias_and_capabilities(tmp_path: Path, monkeypatch):
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    monkeypatch.setenv("BRUNOST_JUDGE_CAPABILITIES", "gpu:true, runtime:docker, gpu:true")
    client = TestClient(create_app(tmp_path / "judge.db"))
    assert client.post("/v1/tasks", json={"task_ref": "agent/v1", "path": str(task)}).status_code == 201
    response = client.post(
        "/v1/evaluations",
        json={"task_ref": "agent/v1", "submission_path": str(submission), "idempotency_key": "eval-1", "evaluation_kind": "agent"},
    )
    assert response.status_code == 202
    evaluation_id = response.json()["evaluation_id"]
    assert client.get(f"/v1/evaluations/{evaluation_id}").status_code == 200
    assert client.get("/v1/workers/capabilities").json()["capabilities"] == ["gpu:true", "runtime:docker"]


def test_worker_registration_heartbeat_and_drain(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "judge.db"))
    registered = client.post(
        "/v1/workers/register",
        json={"worker_id": "gpu-1", "capabilities": ["gpu:true"], "resource_classes": ["gpu"], "region": "nordic"},
    )
    assert registered.status_code == 201
    assert client.get("/v1/workers").json()[0]["worker_id"] == "gpu-1"
    assert client.post("/v1/workers/gpu-1/heartbeat?status=busy").json()["status"] == "busy"
    assert client.post("/v1/workers/gpu-1/drain").json()["draining"] is True
    assert client.get("/v1/workers/capabilities").json()["workers"][0]["capabilities"] == ["gpu:true"]
