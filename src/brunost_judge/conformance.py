"""Small conformance helpers for workers, evaluators, and integrations.

Third-party adapters can run these checks before publishing a plugin.  They do
not depend on FastAPI or a database and therefore work in CI and offline task
authoring environments.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from brunost_judge.contracts import TERMINAL_STATUSES


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
    if score is not None and not isinstance(score, (int, float)):
        errors.append("score must be numeric or null")
    if not isinstance(payload.get("metrics", {}), Mapping):
        errors.append("metrics must be an object")
    return tuple(errors)


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
