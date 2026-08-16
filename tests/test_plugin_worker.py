from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.sandbox import ProcessSandboxRunner
from brunost_judge.server import create_app
from brunost_judge.store import JudgeStore
from brunost_judge.worker import LocalWorker


def test_game_plugin_runs_with_artifact_backed_agents(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS", "true")

    task = tmp_path / "game-task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: game\nrunner: python\n", encoding="utf-8")
    (task / "runner.py").write_text(
        """from pathlib import Path


def run(context):
    participants = context["participants"]
    if set(participants) != {"red", "blue"}:
        return {"status": "failed", "score": 0.0, "failure_reason": "wrong participants"}
    if not all(Path(path).is_dir() for path in participants.values()):
        return {"status": "failed", "score": 0.0, "failure_reason": "participant is not a directory"}
    return {
        "status": "completed",
        "score": 1.0,
        "scores": {"red": 1.0, "blue": 0.0},
        "replay": {"seed": context["seed"]},
        "metrics": {"runner": "reference-game"},
    }
""",
        encoding="utf-8",
    )
    agent_red = tmp_path / "red"
    agent_blue = tmp_path / "blue"
    agent_red.mkdir()
    agent_blue.mkdir()
    (agent_red / "agent.py").write_text("print('red')\n", encoding="utf-8")
    (agent_blue / "agent.py").write_text("print('blue')\n", encoding="utf-8")
    match_assets = tmp_path / "match-assets"
    match_assets.mkdir()
    (match_assets / "seed.txt").write_text("17\n", encoding="utf-8")

    database = tmp_path / "judge.db"
    client = TestClient(create_app(database))
    assert client.post("/v1/tasks", json={"task_ref": "game/v1", "path": str(task)}).status_code == 201
    assert client.post(
        "/v1/agents", json={"agent_id": "red", "name": "Red", "artifact_path": str(agent_red)}
    ).status_code == 201
    assert client.post(
        "/v1/agents", json={"agent_id": "blue", "name": "Blue", "artifact_path": str(agent_blue)}
    ).status_code == 201
    assert client.post(
        "/v1/games",
        json={"game_id": "duel-v1", "name": "Duel", "task_ref": "game/v1", "seats": 2},
    ).status_code == 201
    submitted = client.post(
        "/v1/games/duel-v1/matches",
        json={
            "agent_refs": ["red", "blue"],
            "submission_path": str(match_assets),
            "idempotency_key": "game-plugin-1",
            "seed": 17,
        },
    )
    assert submitted.status_code == 202

    result = LocalWorker(
        JudgeStore(database),
        sandbox_runner=ProcessSandboxRunner(),
    ).process_one()
    assert result is not None
    assert result.status == "completed"
    assert result.score == 1.0
    assert result.metrics["scores"] == {"red": 1.0, "blue": 0.0}
    assert result.metrics["replay"] == {"seed": 17}
