from __future__ import annotations

from pathlib import Path

from brunost_judge.task import validate_task


def _model_task(root: Path, *, baseline: bool = False) -> Path:
    (root / "public" / "datasets").mkdir(parents=True)
    (root / "private" / "datasets").mkdir(parents=True)
    (root / "scorer").mkdir()
    (root / "public" / "datasets" / "train.csv").write_text("x,label\n1,0\n", encoding="utf-8")
    (root / "private" / "datasets" / "test.csv").write_text("x\n2\n", encoding="utf-8")
    (root / "private" / "datasets" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
    (root / "scorer" / "metrics.py").write_text("def evaluate(s, a): return {'private': 1.0}\n", encoding="utf-8")
    if baseline:
        (root / "private" / "baseline.py").write_text("pass\n", encoding="utf-8")
    lines = [
        "version: 1",
        "kind: model",
        "runner: model",
        "runtime: python-3.13-ml-v1",
        "scoring: scorer.metrics:evaluate",
        "network: disabled",
        "time_limit_ms: 125000",
        "training_time_limit_ms: 120000",
        "memory_limit_mb: 2048",
        "public_dataset: public/datasets/train.csv",
        "hidden_dataset: private/datasets/test.csv",
        "hidden_labels_dataset: private/datasets/labels.csv",
        "submission_mode: python_code",
        "submission_language: python",
        "submission_entrypoint: submission.py",
        "prediction_output: predictions.csv",
        "official_split: private",
        f"baseline_enabled: {'true' if baseline else 'false'}",
    ]
    if baseline:
        lines.extend(["baseline_language: python", "baseline_entrypoint: private/baseline.py"])
    (root / "judge.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_validate_model_training_contract_without_baseline(tmp_path: Path):
    result = validate_task(_model_task(tmp_path / "task"))
    assert result.valid, result.errors


def test_validate_model_training_contract_requires_private_labels(tmp_path: Path):
    task = _model_task(tmp_path / "task")
    (task / "private" / "datasets" / "labels.csv").unlink()
    result = validate_task(task)
    assert not result.valid
    assert "labels.csv" in " ".join(result.errors)
