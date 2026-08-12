"""Stable request and result contracts shared by the API, SDK, and worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})


@dataclass(frozen=True)
class TaskRecord:
    task_ref: str
    path: str
    kind: str
    manifest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionRequest:
    task_ref: str
    submission_path: str
    idempotency_key: str
    callback_url: str | None = None
    callback_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    task_ref: str
    status: str
    score: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    judge_version: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
