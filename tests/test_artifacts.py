import io
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.artifacts import (
    ArtifactError,
    artifact_id,
    pack_directory,
    safe_extract,
)
from brunost_judge.server import create_app


def _task(root: Path) -> Path:
    task = root / "task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    return task


def test_artifact_store_rejects_traversal(tmp_path: Path):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"nope"))
    try:
        safe_extract(output.getvalue(), tmp_path / "out")
    except ArtifactError:
        pass
    else:
        raise AssertionError("path traversal was accepted")


def test_directory_bundle_is_deterministic(tmp_path: Path):
    task = _task(tmp_path)
    assert pack_directory(task) == pack_directory(task)


def test_task_and_submission_artifacts_remove_shared_mount_requirement(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    client = TestClient(create_app(tmp_path / "judge.db"))
    task_data = pack_directory(_task(tmp_path))
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "answer.txt").write_text("answer\n", encoding="utf-8")
    submission_data = pack_directory(submission)
    task_id = artifact_id(task_data)
    submission_id = artifact_id(submission_data)

    assert client.put(f"/v1/artifacts/{task_id}", content=task_data).status_code == 201
    assert client.put(f"/v1/artifacts/{submission_id}", content=submission_data).status_code == 201
    registered = client.post("/v1/tasks", json={"task_ref": "artifact/ioai-v1", "artifact_id": task_id})
    assert registered.status_code == 201
    assert registered.json()["path"] == f"artifact://{task_id}"
    submitted = client.post(
        "/v1/evaluations",
        json={"task_ref": "artifact/ioai-v1", "submission_artifact_id": submission_id, "idempotency_key": "artifact-attempt"},
    )
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "queued"
