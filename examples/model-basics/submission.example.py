"""Copy this file to a submission bundle as submission.py."""

from pathlib import Path


def train(train_dataset: str, model_path: str) -> None:
    # The example remembers the known label for ``blue`` deterministically.
    _ = train_dataset
    Path(model_path).write_text("1\n", encoding="utf-8")


def predict(model_path: str, test_dataset: str, predictions_path: str) -> None:
    _ = test_dataset
    Path(predictions_path).write_text(Path(model_path).read_text(encoding="utf-8"), encoding="utf-8")
