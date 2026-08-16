"""Small conformance helpers for workers, evaluators, and integrations.

Third-party adapters can run these checks before publishing a plugin.  They do
not depend on FastAPI or a database and therefore work in CI and offline task
authoring environments.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brunost_judge.contracts import RESULT_SCHEMA_VERSION, TERMINAL_STATUSES


def validate_result_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return actionable errors for a canonical evaluation result."""
    errors: list[str] = []
    if not payload.get("execution_id") and not payload.get("evaluation_id"):
        errors.append("missing evaluation_id")
    if not payload.get("task_ref"):
        errors.append("missing task_ref")
    status = payload.get("status")
    if status not in {*TERMINAL_STATUSES, "queued", "running"}:
        errors.append("status must be queued, running, completed, failed, or canceled")
    score = payload.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
        errors.append("score must be numeric or null")
    elif score is not None and not math.isfinite(float(score)):
        errors.append("score must be finite")
    if not isinstance(payload.get("metrics", {}), Mapping):
        errors.append("metrics must be an object")
    _validate_scores(payload.get("scores"), errors, prefix="scores")
    winner = payload.get("winner")
    if winner is not None and (not isinstance(winner, str) or not winner):
        errors.append("winner must be a non-empty string or null")
    _validate_artifacts(payload.get("artifacts", {}), errors, prefix="artifacts", references=True)
    result_version = payload.get("result_version")
    if result_version is not None and result_version != RESULT_SCHEMA_VERSION:
        errors.append(f"result_version must be {RESULT_SCHEMA_VERSION}")
    return tuple(errors)


def validate_runner_result_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the reduced result emitted by an evaluator sandbox."""
    errors: list[str] = []
    if payload.get("status") not in {"completed", "failed"}:
        errors.append("sandbox result status must be completed or failed")
    score = payload.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
        errors.append("sandbox result score must be numeric or null")
    elif score is not None and not math.isfinite(float(score)):
        errors.append("sandbox result score must be finite")
    if not isinstance(payload.get("metrics", {}), Mapping):
        errors.append("sandbox result metrics must be an object")
    _validate_scores(payload.get("scores"), errors, prefix="sandbox result scores")
    winner = payload.get("winner")
    if winner is not None and (not isinstance(winner, str) or not winner):
        errors.append("sandbox result winner must be a non-empty string or null")
    _validate_artifacts(payload.get("artifacts", {}), errors, prefix="sandbox result artifacts")
    if payload.get("status") == "failed" and not isinstance(payload.get("failure_reason"), str):
        errors.append("failed sandbox results need a failure_reason")
    result_version = payload.get("result_version")
    if result_version is not None and result_version != RESULT_SCHEMA_VERSION:
        errors.append(f"result_version must be {RESULT_SCHEMA_VERSION}")
    return tuple(errors)


def _validate_scores(value: Any, errors: list[str], *, prefix: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    for seat, score in value.items():
        if not isinstance(seat, str) or not seat:
            errors.append(f"{prefix} keys must be non-empty strings")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            errors.append(f"{prefix} values must be numeric")
        elif not math.isfinite(float(score)):
            errors.append(f"{prefix} values must be finite")


def _validate_artifacts(value: Any, errors: list[str], *, prefix: str, references: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    if len(value) > 32:
        errors.append(f"{prefix} contains too many entries")
    for name, descriptor in value.items():
        if not isinstance(name, str) or not name:
            errors.append(f"{prefix} names must be non-empty strings")
            continue
        if references:
            if not isinstance(descriptor, Mapping) or not isinstance(descriptor.get("artifact_id"), str):
                errors.append(f"{prefix}.{name} must declare an artifact_id")
            continue
        if isinstance(descriptor, str):
            path = descriptor
        elif isinstance(descriptor, Mapping):
            path = descriptor.get("path")
        else:
            path = None
        if not isinstance(path, str) or not path:
            errors.append(f"{prefix}.{name} must declare a path")
            continue
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"{prefix}.{name} path must be relative")


def validate_capability_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the normalized worker capability response."""
    errors: list[str] = []
    if not payload.get("worker_id"):
        errors.append("missing worker_id")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) and item for item in capabilities):
        errors.append("capabilities must be a list of non-empty strings")
    return tuple(errors)


def assert_conformant(payload: Mapping[str, Any], *, kind: str) -> None:
    """Raise ``ValueError`` when a plugin response violates the public seam."""
    if kind == "result":
        errors = validate_result_payload(payload)
    elif kind == "capabilities":
        errors = validate_capability_payload(payload)
    else:
        raise ValueError(f"unknown conformance payload kind: {kind}")
    if errors:
        raise ValueError("; ".join(errors))
