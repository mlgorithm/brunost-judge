"""Tiny public CPU-only smoke-test scorer.

This example deliberately has no hidden answer. Real contest tasks may read
private labels/assets from ``assets_path/private`` but must keep them out of
the public repository and test fixtures.
"""

from __future__ import annotations

from pathlib import Path


def evaluate(submission_path: str, assets_path: str) -> dict[str, float]:
    _ = assets_path
    actual = (Path(submission_path) / "answer.txt").read_text(encoding="utf-8").strip()
    score = 1.0 if actual else 0.0
    return {"public": score}
