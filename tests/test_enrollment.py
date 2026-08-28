from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.server import create_app


def _task(root: Path) -> Path:
    task = root / "task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    return task


def test_enrollment_is_one_time_and_worker_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}

    issued = client.post(
        "/v1/nodes/enrollment-tokens",
        headers=admin,
        json={
            "node_id": "node-2",
            "worker_id": "cpu-2",
            "capabilities": ["runtime:docker", "gpu:true"],
            "resource_classes": ["cpu", "gpu"],
            "region": "north",
        },
    )
    assert issued.status_code == 201
    join_token = issued.json()["join_token"]

    enrolled = client.post(
        "/v1/nodes/enroll",
        json={"join_token": join_token, "hostname": "judge-node-2", "capabilities": ["gpu:true"], "resource_classes": ["gpu"]},
    )
    assert enrolled.status_code == 201
    payload = enrolled.json()
    assert payload["worker"]["worker_id"] == "cpu-2"
    assert payload["worker"]["metadata"]["hostname"] == "judge-node-2"
    assert "gpu:true" in payload["worker"]["capabilities"]
    assert "gpu" in payload["worker"]["resource_classes"]
    worker_headers = {"Authorization": f"Bearer {payload['worker_token']}"}
    assert client.get("/v1/workers/cpu-2/status", headers=worker_headers).status_code == 200
    assert client.get("/v1/workers/cpu-2/status", headers={"Authorization": "Bearer wrong"}).status_code == 401

    assert client.post("/v1/workers/cpu-2/credential/revoke", headers=admin).json() == {"worker_id": "cpu-2", "revoked": True}
    assert client.get("/v1/workers/cpu-2/status", headers=worker_headers).status_code == 401

    replay = client.post("/v1/nodes/enroll", json={"join_token": join_token})
    assert replay.status_code == 401


def test_enrolled_worker_claims_and_finishes_only_its_lease(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}

    assert client.post("/v1/tasks", headers=admin, json={"task_ref": "ioai/v1", "path": str(task)}).status_code == 201
    issued = client.post("/v1/nodes/enrollment-tokens", headers=admin, json={"node_id": "node-1", "worker_id": "cpu-1"})
    enrolled = client.post("/v1/nodes/enroll", json={"join_token": issued.json()["join_token"]})
    worker_token = enrolled.json()["worker_token"]
    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    execution = client.post(
        "/v1/evaluations",
        headers=admin,
        json={"task_ref": "ioai/v1", "submission_path": str(submission), "idempotency_key": "attempt-1"},
    ).json()

    claimed = client.post("/v1/workers/cpu-1/claim", headers=worker_headers)
    assert claimed.status_code == 200
    assert claimed.json()["execution"]["execution_id"] == execution["execution_id"]
    finish = client.post(
        "/v1/workers/cpu-1/finish",
        headers=worker_headers,
        json={
            "execution_id": execution["execution_id"],
            "task_ref": "ioai/v1",
            "status": "completed",
            "score": 1.0,
        },
    )
    assert finish.status_code == 200
    assert finish.json()["status"] == "completed"
    assert client.post("/v1/workers/cpu-1/claim", headers=worker_headers).status_code == 204


def test_node_cannot_elevate_capabilities_beyond_operator_grant(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}
    issued = client.post(
        "/v1/nodes/enrollment-tokens",
        headers=admin,
        json={"node_id": "node-3", "worker_id": "cpu-3", "capabilities": ["runtime:docker"]},
    )
    response = client.post(
        "/v1/nodes/enroll",
        json={"join_token": issued.json()["join_token"], "capabilities": ["gpu:true"]},
    )
    assert response.status_code == 422


def test_enrollment_uses_the_node_reported_capability_subset(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}
    issued = client.post(
        "/v1/nodes/enrollment-tokens",
        headers=admin,
        json={
            "node_id": "node-4",
            "worker_id": "cpu-4",
            "capabilities": ["gpu:true", "runtime:docker"],
            "resource_classes": ["cpu", "gpu"],
        },
    )

    enrolled = client.post(
        "/v1/nodes/enroll",
        json={
            "join_token": issued.json()["join_token"],
            "capabilities": ["runtime:docker"],
            "resource_classes": ["cpu"],
        },
    )
    assert enrolled.status_code == 201
    worker = enrolled.json()["worker"]
    assert worker["capabilities"] == ["runtime:docker"]
    assert worker["resource_classes"] == ["cpu"]


def test_canceled_execution_cannot_be_completed_or_erase_provenance(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "admin-secret")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    client = TestClient(create_app(tmp_path / "judge.db"))
    admin = {"Authorization": "Bearer admin-secret"}
    assert client.post("/v1/tasks", headers=admin, json={"task_ref": "ioai/v1", "path": str(task)}).status_code == 201
    issued = client.post("/v1/nodes/enrollment-tokens", headers=admin, json={"node_id": "node-5", "worker_id": "cpu-5"})
    enrolled = client.post("/v1/nodes/enroll", json={"join_token": issued.json()["join_token"]})
    worker_headers = {"Authorization": f"Bearer {enrolled.json()['worker_token']}"}
    execution = client.post(
        "/v1/evaluations",
        headers=admin,
        json={"task_ref": "ioai/v1", "submission_path": str(submission), "idempotency_key": "cancel-race"},
    ).json()
    assert client.post("/v1/workers/cpu-5/claim", headers=worker_headers).status_code == 200
    assert client.post(
        f"/v1/workers/cpu-5/executions/{execution['execution_id']}/lease",
        headers=worker_headers,
    ).json()["renewed"] is True
    assert client.post(f"/v1/executions/{execution['execution_id']}/cancel", headers=admin).status_code == 200
    finished = client.post(
        "/v1/workers/cpu-5/finish",
        headers=worker_headers,
        json={
            "execution_id": execution["execution_id"],
            "task_ref": "ioai/v1",
            "status": "completed",
            "score": 1.0,
            "metadata": {},
        },
    )
    assert finished.status_code == 200
    payload = finished.json()
    assert payload["status"] == "canceled"
    assert payload["score"] is None
    assert payload["metadata"]["event_id"].startswith("execution:")
