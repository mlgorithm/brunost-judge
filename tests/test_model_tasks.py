from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from brunost_judge.artifacts import artifact_id, pack_directory
from brunost_judge.server import create_app
from brunost_judge.task import validate_task


def _model_task(root: Path, *, baseline: bool = False, post: bool = False) -> Path:
    (root / "public" / "datasets").mkdir(parents=True)
    (root / "private" / "datasets").mkdir(parents=True)
    (root / "private" / "post" / "training").mkdir(parents=True)
    (root / "private" / "post" / "test").mkdir(parents=True)
    (root / "private" / "post" / "labels").mkdir(parents=True)
    (root / "evaluator.py").write_text(
        "def evaluate(predictions_path, labels_path):\n    return 1.0\n", encoding="utf-8"
    )
    (root / "public" / "datasets" / "train.csv").write_text("x,label\n1,0\n", encoding="utf-8")
    (root / "private" / "datasets" / "test.csv").write_text("x\n2\n", encoding="utf-8")
    (root / "private" / "datasets" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
    lines = [
        "version: 2",
        "kind: model",
        "runner: model",
        "model_contract: train_predict_v2",
        "runtime: python-3.13-ml-v1",
        "evaluation: evaluator:evaluate",
        "network: disabled",
        "time_limit_ms: 1500000",
        "training_time_limit_ms: 120000",
        "prediction_time_limit_ms: 10000",
        "evaluator_time_limit_ms: 10000",
        "memory_limit_mb: 2048",
        "model_max_bytes: 64000000",
        "training_dataset: public/datasets/train.csv",
        "private_test_dataset: private/datasets/test.csv",
        "private_labels_dataset: private/datasets/labels.csv",
        "submission_entrypoint: submission.py",
        f"baseline_enabled: {'true' if baseline else 'false'}",
        "post_competition_enabled: true" if post else "post_competition_enabled: false",
    ]
    if baseline:
        (root / "private" / "baseline.py").write_text(
            "def train(train_dataset, model_path):\n    pass\n\ndef predict(model_path, test_dataset, predictions_path):\n    pass\n",
            encoding="utf-8",
        )
        lines.append("baseline_entrypoint: private/baseline.py")
    if post:
        (root / "private" / "post" / "training" / "training.csv").write_text("x,label\n3,0\n", encoding="utf-8")
        (root / "private" / "post" / "test" / "test.csv").write_text("x\n4\n", encoding="utf-8")
        (root / "private" / "post" / "labels" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
        (root / "post_evaluator.py").write_text(
            "def evaluate(predictions_path, labels_path):\n    return 0.5\n", encoding="utf-8"
        )
        lines.extend([
            "post_training_dataset: private/post/training/training.csv",
            "post_test_dataset: private/post/test/test.csv",
            "post_labels_dataset: private/post/labels/labels.csv",
            "post_training_time_limit_ms: 600000",
            "post_prediction_time_limit_ms: 10000",
            "post_evaluator_time_limit_ms: 60000",
            "post_evaluator_entrypoint: post_evaluator.py",
        ])
    (root / "judge.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_validate_model_training_contract_without_baseline(tmp_path: Path):
    result = validate_task(_model_task(tmp_path / "task"))
    assert result.valid, result.errors
    assert result.settings["runtime"] == "python-3.13-ml-v1"
    assert result.settings["required_capabilities"] == ["runtime:python-3.13-ml-v1"]


def test_validate_model_training_contract_with_baseline_and_post_profile(tmp_path: Path):
    result = validate_task(_model_task(tmp_path / "task", baseline=True, post=True))
    assert result.valid, result.errors


def test_validate_model_training_contract_requires_private_labels(tmp_path: Path):
    task = _model_task(tmp_path / "task")
    (task / "private" / "datasets" / "labels.csv").unlink()
    result = validate_task(task)
    assert not result.valid
    assert "private_labels_dataset" in " ".join(result.errors)


def test_validate_model_rejects_legacy_manifest(tmp_path: Path):
    task = _model_task(tmp_path / "task")
    manifest = (task / "judge.yaml").read_text(encoding="utf-8")
    manifest = manifest.replace("version: 2", "version: 1").replace(
        "model_contract: train_predict_v2", "scoring: scorer.metrics:evaluate"
    )
    (task / "judge.yaml").write_text(
        manifest + "submission_mode: python_code\nprediction_output: predictions.csv\n", encoding="utf-8"
    )
    result = validate_task(task)
    assert not result.valid
    assert "unsupported judge.yaml version" in " ".join(result.errors)
    assert "model_contract" in " ".join(result.errors)


def test_model_artifact_registration_uses_package_identity_fields(tmp_path: Path, monkeypatch):
    """An immutable task ref cannot be accidentally registered as v1/classic."""

    monkeypatch.setenv("BRUNOST_JUDGE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    bundle = pack_directory(_model_task(tmp_path / "task"))
    identifier = artifact_id(bundle)
    client = TestClient(create_app(tmp_path / "judge.db"))

    assert client.put(f"/v1/artifacts/{identifier}", content=bundle).status_code == 201
    registered = client.post(
        "/v1/tasks",
        json={"task_ref": "model/v2", "artifact_id": identifier},
    )
    assert registered.status_code == 201
    manifest = registered.json()["manifest"]
    assert manifest["kind"] == "model"
    assert manifest["version"] == 2
    assert manifest["runtime"] == "python-3.13-ml-v1"
    assert manifest["evaluator"] == "evaluator:evaluate"

    mismatched = client.post(
        "/v1/tasks",
        json={"task_ref": "model/v2-mismatched", "artifact_id": identifier, "version": 1},
    )
    assert mismatched.status_code == 422
    assert "version" in mismatched.json()["detail"]
