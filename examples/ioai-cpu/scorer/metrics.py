"""Tiny CPU-only example task used by the package smoke test."""

from __future__ import annotations

from pathlib import Path


def evaluate(submission_path: str, assets_path: str) -> dict[str, float]:
    expected = (Path(assets_path) / "private" / "answer.txt").read_text(encoding="utf-8").strip()
    actual = (Path(submission_path) / "answer.txt").read_text(encoding="utf-8").strip()
    score = 1.0 if actual == expected else 0.0
    return {"public": score}
