"""Execute one v2 ML evaluator with only predictions and labels."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


def _failed(reason: str) -> dict[str, Any]:
    return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": reason[:2000]}


def _load_module(entrypoint: str):
    path = Path(entrypoint).resolve()
    spec = importlib.util.spec_from_file_location(f"brunost_evaluator_{os.getpid()}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("evaluator module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _score_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bool):
        return _failed("evaluator returned a boolean, not a numeric score")
    metrics: dict[str, Any] = {}
    if isinstance(raw, (int, float)):
        score = raw
    elif isinstance(raw, dict):
        if "public" in raw or "private" in raw or "score" not in raw:
            return _failed("v2 evaluators must return a numeric score or {'score': number, 'metrics': {...}}")
        if "metrics" in raw and not isinstance(raw["metrics"], dict):
            return _failed("evaluator metrics must be an object")
        score = raw.get("score")
        metrics = raw.get("metrics", {})
    else:
        return _failed(f"evaluator returned unsupported type {type(raw).__name__}")
    try:
        score = float(score)
    except (TypeError, ValueError):
        return _failed("evaluator returned a non-numeric score")
    if not math.isfinite(score):
        return _failed("evaluator returned a non-finite score")
    metrics.setdefault("score", score)
    try:
        json.dumps(metrics, allow_nan=False)
    except (TypeError, ValueError):
        return _failed("evaluator metrics must be JSON serializable")
    return {"status": "completed", "score": score, "metrics": metrics}


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("entrypoint")
    parser.add_argument("predictions_path")
    parser.add_argument("labels_path")
    args = parser.parse_args()
    module = _load_module(args.entrypoint)
    function = getattr(module, "evaluate", None)
    if not callable(function):
        raise TypeError("evaluator.py must define evaluate(predictions_path, labels_path)")
    return _score_result(function(args.predictions_path, args.labels_path))


if __name__ == "__main__":
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = main()
    except Exception as exc:  # noqa: BLE001 - the harness reports a structured failure
        result = _failed(f"Evaluator raised {type(exc).__name__}: {exc}")
    print(json.dumps(result, separators=(",", ":")))
