import io
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.artifacts import (
    ArtifactError,
    artifact_id,
    artifact_store_from_environment,
    pack_directory,
    safe_extract,
)
from brunost_judge.server import create_app
from brunost_judge.store import JudgeStore
from brunost_judge.worker import LocalWorker


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


def test_artifact_store_rejects_archive_bombs(tmp_path: Path):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("large.bin")
        info.size = 8
        archive.addfile(info, io.BytesIO(b"12345678"))
    try:
        safe_extract(output.getvalue(), tmp_path / "out", max_member_bytes=4)
    except ArtifactError:
        pass
    else:
        raise AssertionError("oversized archive member was accepted")


def test_directory_bundle_is_deterministic(tmp_path: Path):
    task = _task(tmp_path)
    assert pack_directory(task) == pack_directory(task)


def test_generated_bytecode_is_not_part_of_artifact_or_task_digest(tmp_path: Path):
    task = _task(tmp_path)
    from brunost_judge.task import task_digest

    before_bundle = pack_directory(task)
    before_digest = task_digest(task)
    cache = task / "scorer" / "__pycache__"
    cache.mkdir()
    (cache / "metrics.cpython-314.pyc").write_bytes(b"generated")

    assert pack_directory(task) == before_bundle
    assert task_digest(task) == before_digest


def test_artifact_store_from_environment_defaults_to_filesystem(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("BRUNOST_JUDGE_ARTIFACT_BACKEND", raising=False)
    store = artifact_store_from_environment()
    assert type(store).__name__ == "ArtifactStore"


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


def test_registered_task_is_snapshot_and_worker_verifies_digest(tmp_path: Path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("BRUNOST_JUDGE_ARTIFACT_ROOT", str(artifact_root))
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    client = TestClient(create_app(tmp_path / "judge.db"))
    registered = client.post("/v1/tasks", json={"task_ref": "immutable/v1", "path": str(task)})
    assert registered.status_code == 201
    assert registered.json()["path"].startswith("artifact://")
    original_digest = registered.json()["manifest"]["digest"]
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 0.0\n", encoding="utf-8")
    queued = client.post(
        "/v1/evaluations",
        json={"task_ref": "immutable/v1", "submission_path": str(submission), "idempotency_key": "immutable-1"},
    )
    assert queued.status_code == 202

    class CaptureRunner:
        def run(self, submission_path: Path, task_path: Path, execution_id: str) -> dict:
            _ = submission_path, execution_id
            assert task_digest(task_path) == original_digest
            return {"status": "completed", "score": 1.0, "metrics": {}}

    from brunost_judge.task import task_digest

    result = LocalWorker(JudgeStore(tmp_path / "judge.db"), sandbox_runner=CaptureRunner()).process_one()
    assert result is not None
    assert result.status == "completed"
