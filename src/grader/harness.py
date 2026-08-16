"""Scorer harness: run a task's metrics.py and normalize its output.

Pure standard-library (the task's metrics.py may use numpy/pandas; the harness itself
does not). No Brunost backend imports — this is the extraction seam (ADR-0010).
"""

from __future__ import annotations

import importlib.util
import math
import os
import traceback
from pathlib import Path
from typing import Any

_MAX_REASON_CHARS = 2000


def _finite_float(value: Any) -> float | None:
    """Coerce to a finite float, or None if it isn't a real number."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normalize_result(raw: Any) -> dict[str, Any]:
    """Map whatever a task scorer returned into the canonical result contract.

    Returns {"status", "score", "metrics"[, "failure_reason"]}. `score` is the PUBLIC
    value (safe to show live); the private value lives only in metrics["private"].
    Accepts: a number; {"public","private","*_detail"}; or the IOAI
    {"score": {"public_a","private_b","*_detail"}} shape.
    """
    metrics: dict[str, Any] = {}
    public: float | None = None
    private: float | None = None

    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return _failed("Scorer returned a boolean, not a score")
    if isinstance(raw, (int, float)):
        public = _finite_float(raw)
    elif isinstance(raw, dict):
        if isinstance(raw.get("metrics"), dict):
            metrics.update(raw["metrics"])
        score_field = raw.get("score")
        if isinstance(score_field, dict):  # IOAI nested shape
            public = _finite_float(score_field.get("public_a", score_field.get("public")))
            private = _finite_float(score_field.get("private_b", score_field.get("private")))
            for key in ("public_detail", "private_detail"):
                if isinstance(score_field.get(key), dict):
                    metrics[key] = score_field[key]
        elif score_field is not None:
            public = _finite_float(score_field)
        # flat top-level keys win if present
        if raw.get("public") is not None:
            public = _finite_float(raw["public"])
        if raw.get("private") is not None:
            private = _finite_float(raw["private"])
        for key in ("public_detail", "private_detail"):
            if isinstance(raw.get(key), dict):
                metrics[key] = raw[key]
    else:
        return _failed(f"Scorer returned unsupported type {type(raw).__name__}")

    if public is not None:
        metrics["public"] = public
    if private is not None:
        metrics["private"] = private

    score = public if public is not None else private
    if score is None:
        return _failed("Scorer returned no usable numeric score", metrics=metrics)
    return {"status": "completed", "score": score, "metrics": metrics}


def _failed(reason: str, *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "failed",
        "score": 0.0,
        "metrics": metrics or {},
        "failure_reason": reason[:_MAX_REASON_CHARS],
    }


def _load_metrics_module(assets_path: str):
    """Import metrics.py from either the legacy or packaged task layout."""
    candidates = (
        os.path.join(assets_path, "metrics.py"),
        os.path.join(assets_path, "scorer", "metrics.py"),
    )
    metrics_file = next((path for path in candidates if os.path.isfile(path)), None)
    if metrics_file is None:
        return None
    spec = importlib.util.spec_from_file_location("task_metrics", metrics_file)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Permit a packaged scorer to import helper modules beside metrics.py while
    # keeping the import scoped to this task invocation.
    search_paths = [os.path.dirname(metrics_file), assets_path]
    original_path = list(__import__("sys").path)
    __import__("sys").path[:0] = search_paths
    try:
        spec.loader.exec_module(module)
    finally:
        __import__("sys").path[:] = original_path
    return module


def _task_kind(assets_path: str) -> str:
    manifest_path = os.path.join(assets_path, "judge.yaml")
    if not os.path.isfile(manifest_path):
        return ""
    with open(manifest_path, encoding="utf-8") as manifest_file:
        return next(
            (
                line.split(":", 1)[1].strip().strip("\"'").lower()
                for line in manifest_file
                if line.strip().startswith("kind:")
            ),
            "",
        )


def _run_plugin(submission_path: str, assets_path: str) -> dict[str, Any]:
    from grader.plugins import RunnerContext, default_registry, read_context_manifest

    manifest = read_context_manifest(submission_path)
    kind = "match" if manifest.get("evaluation_kind") == "match" else "agent"
    participants = manifest.get("participants", {})
    if not isinstance(participants, dict):
        participants = {}
    root = Path(submission_path)
    resolved_participants = {
        str(agent_id): str((root / str(relative)).resolve())
        for agent_id, relative in participants.items()
        if isinstance(agent_id, str) and isinstance(relative, str)
    }
    seats = []
    for item in manifest.get("seats", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        seat = dict(item)
        seat["path"] = str((root / seat["path"]).resolve())
        seats.append(seat)
    context = RunnerContext(
        execution_id=str(manifest.get("execution_id") or "local-plugin-execution"),
        task_ref=str(manifest.get("task_ref") or "local-plugin-task"),
        evaluation_kind=kind,
        task_path=str(Path(assets_path).resolve()),
        submission_path=str(root.resolve()),
        participants=resolved_participants,
        seats=tuple(seats),
        seed=manifest.get("seed") if isinstance(manifest.get("seed"), int) else None,
        metadata=manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {},
    )
    return default_registry().run(kind, context)


def run(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Score a submission with the task's metrics.py; never raise.

    Any failure (missing scorer, bad submission, scorer exception) returns a
    'failed' result with a reason instead of crashing the sandbox.
    """
    try:
        kind = _task_kind(assets_path)
        if kind in {"icpc", "ioi", "interactive"}:
            from grader.classic import run_classic, run_interactive

            runner = run_interactive if kind == "interactive" else run_classic
            return runner(submission_path, assets_path)
        if kind in {"agent", "game"}:
            return _run_plugin(submission_path, assets_path)
        module = _load_metrics_module(assets_path)
        if module is None:
            return _failed("Task has no metrics.py scorer in its assets")
        evaluate = getattr(module, "evaluate", None)
        if not callable(evaluate):
            return _failed("Task metrics.py must define evaluate(submission_path, assets_path)")
        raw = evaluate(submission_path, assets_path)
        return normalize_result(raw)
    except Exception as exc:  # noqa: BLE001 — the harness must contain all scorer errors
        detail = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=3)
        return _failed(f"Scorer raised an exception — {detail}\n{tb}")
