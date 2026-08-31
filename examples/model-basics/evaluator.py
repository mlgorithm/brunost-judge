"""Minimal author-owned evaluator for the model basics example."""

from pathlib import Path


def evaluate(predictions_path: str, labels_path: str) -> float:
    predictions = Path(predictions_path).read_text(encoding="utf-8").splitlines()
    labels = Path(labels_path).read_text(encoding="utf-8").splitlines()
    return 1.0 if predictions == labels else 0.0
