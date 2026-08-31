"""Stable request and result contracts shared by the API, SDK, and worker.

The contracts deliberately use plain dataclasses so task authors and platform
integrators do not need to install a web framework.  The HTTP layer mirrors
these shapes with JSON and keeps backwards compatibility with the original
``ExecutionRequest``/``ExecutionResult`` names.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
RESULT_SCHEMA_VERSION = 1
RESOURCE_PROFILE_MODE = "planning_only"


@dataclass(frozen=True)
class TaskRecord:
    task_ref: str
    path: str
    kind: str
    manifest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceProfile:
    """Portable resource planning metadata, not a scheduling guarantee.

    The Judge currently selects workers using ``resource_class`` and
    ``required_capabilities``.  It neither admits work nor applies sandbox
    limits from this profile, so callers must not treat these values as
    enforced resource requirements.
    """

    cpu_cores: float = 1
    memory_mb: int = 512
    gpu_count: int = 0
    gpu_memory_mb: int = 0
    ephemeral_storage_mb: int = 1024

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskDefinition:
    """Versioned task metadata exposed to platform integrations."""

    task_ref: str
    kind: str
    path: str | None = None
    version: int = 1
    runtime: str = "python-3.13"
    evaluator: str | None = None
    resource_profile: ResourceProfile = field(default_factory=ResourceProfile)
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["resource_profile"] = self.resource_profile.as_dict()
        payload["resource_profile_mode"] = RESOURCE_PROFILE_MODE
        payload["required_capabilities"] = list(self.required_capabilities)
        return payload


@dataclass(frozen=True)
class AgentDefinition:
    """A versioned contestant or baseline agent used by agent evaluations."""

    agent_id: str
    name: str
    version: str = "1"
    artifact_path: str | None = None
    protocol: str = "stdio"
    resource_profile: ResourceProfile = field(default_factory=ResourceProfile)
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["resource_profile"] = self.resource_profile.as_dict()
        payload["resource_profile_mode"] = RESOURCE_PROFILE_MODE
        payload["required_capabilities"] = list(self.required_capabilities)
        return payload


@dataclass(frozen=True)
class GameDefinition:
    """A referee-backed game definition used to create match evaluations."""

    game_id: str
    name: str
    task_ref: str
    seats: int = 2
    protocol: str = "stdio"
    referee: str | None = None
    resource_profile: ResourceProfile = field(default_factory=ResourceProfile)
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["resource_profile"] = self.resource_profile.as_dict()
        payload["resource_profile_mode"] = RESOURCE_PROFILE_MODE
        payload["required_capabilities"] = list(self.required_capabilities)
        return payload


@dataclass(frozen=True)
class DefinitionRecord:
    """Durable registry entry for extensible agent and game definitions."""

    definition_type: str
    definition_id: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "definition_type": self.definition_type,
            "definition_id": self.definition_id,
            **self.payload,
        }


@dataclass(frozen=True)
class WorkerRecord:
    """A worker advertisement stored by the judge control plane."""

    worker_id: str
    capabilities: tuple[str, ...] = ()
    queues: tuple[str, ...] = ("default",)
    resource_classes: tuple[str, ...] = ("cpu",)
    region: str | None = None
    status: str = "ready"
    draining: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    last_seen: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("capabilities", "queues", "resource_classes"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class ExecutionRequest:
    task_ref: str
    submission_path: str
    idempotency_key: str
    callback_url: str | None = None
    callback_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    queue: str = "default"
    resource_class: str = "cpu"
    priority: int = 0
    timeout_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationRequest:
    """Canonical high-level request for any judge evaluation.

    ``evaluation_kind`` is one of ``batch``, ``interactive``, ``agent``, or
    ``match``. The built-in distribution executes scorer-backed batch tasks,
    deterministic coding tasks, line-oriented interactive tasks, and versioned
    agent/game runner plugins. Unsupported future kinds fail closed instead of
    silently being treated as batch scores.
    """

    task_ref: str
    submission_path: str
    idempotency_key: str
    evaluation_kind: str = "batch"
    agent_refs: tuple[str, ...] = ()
    game_ref: str | None = None
    seed: int | None = None
    callback_url: str | None = None
    callback_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    queue: str = "default"
    resource_class: str = "cpu"
    priority: int = 0
    timeout_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["agent_refs"] = list(self.agent_refs)
        return payload

    def to_execution_request(self) -> ExecutionRequest:
        metadata = dict(self.metadata)
        metadata.update({"evaluation_kind": self.evaluation_kind})
        if self.agent_refs:
            metadata["agent_refs"] = list(self.agent_refs)
        if self.game_ref:
            metadata["game_ref"] = self.game_ref
        if self.seed is not None:
            metadata["seed"] = self.seed
        return ExecutionRequest(
            task_ref=self.task_ref,
            submission_path=self.submission_path,
            idempotency_key=self.idempotency_key,
            callback_url=self.callback_url,
            callback_token=self.callback_token,
            metadata=metadata,
            queue=self.queue,
            resource_class=self.resource_class,
            priority=self.priority,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    task_ref: str
    status: str
    score: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    result_version: int = RESULT_SCHEMA_VERSION
    judge_version: str = "local"
    queue: str = "default"
    resource_class: str = "cpu"
    priority: int = 0
    task_digest: str | None = None
    evaluator: str | None = None
    runtime_image: str | None = None
    seed: int | None = None
    event_id: str | None = None
    scores: dict[str, float] = field(default_factory=dict)
    winner: str | None = None
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # ``evaluation_id`` is the stable public name.  Keep ``execution_id``
        # for clients of the original 0.x API.
        payload["evaluation_id"] = self.execution_id
        return payload
