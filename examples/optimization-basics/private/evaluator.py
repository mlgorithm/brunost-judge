"""Trusted evaluator for the optimization basics example."""

from pathlib import Path


def evaluate(input_path: str, output_path: str) -> dict:
    capacity = int(Path(input_path).read_text(encoding="utf-8").strip())
    candidate = int(Path(output_path).read_text(encoding="utf-8").strip())
    return {
        "feasible": 0 <= candidate <= capacity,
        "objective": candidate,
        "score": candidate / capacity if capacity else 1.0,
    }
