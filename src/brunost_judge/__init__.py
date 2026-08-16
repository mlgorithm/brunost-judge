"""Public, platform-independent Brunost Judge API.

The compatibility ``grader`` package remains importable for task packages that
were authored before the standalone repository was extracted.
"""

from brunost_judge.adapters import (
    DockerAdapter,
    KubernetesAdapter,
    LaunchPlan,
    LaunchRequest,
    OpenStackAdapter,
    SlurmAdapter,
)
from brunost_judge.artifacts import (
    ArtifactError,
    ArtifactStore,
    artifact_id,
    pack_directory,
    safe_extract,
)
from brunost_judge.conformance import (
    assert_conformant,
    validate_capability_payload,
    validate_result_payload,
    validate_runner_result_payload,
)
from brunost_judge.contracts import (
    RESULT_SCHEMA_VERSION,
    AgentDefinition,
    EvaluationRequest,
    ExecutionRequest,
    ExecutionResult,
    GameDefinition,
    ResourceProfile,
    TaskDefinition,
    WorkerRecord,
)
from brunost_judge.games import AgentSeat, GameRunner, MatchRequest, MatchResult
from brunost_judge.plugins import (
    PLUGIN_KINDS,
    PLUGIN_PROTOCOL_VERSION,
    RunnerContext,
    RunnerPlugin,
    RunnerRegistry,
)
from brunost_judge.scheduler import (
    CapabilityScheduler,
    SchedulingRequest,
    WorkerAdvertisement,
)
from brunost_judge.task import task_digest
from grader.harness import normalize_result, run

__all__ = [
    "PLUGIN_KINDS",
    "PLUGIN_PROTOCOL_VERSION",
    "RESULT_SCHEMA_VERSION",
    "AgentDefinition",
    "AgentSeat",
    "ArtifactError",
    "ArtifactStore",
    "CapabilityScheduler",
    "DockerAdapter",
    "EvaluationRequest",
    "ExecutionRequest",
    "ExecutionResult",
    "GameDefinition",
    "GameRunner",
    "KubernetesAdapter",
    "LaunchPlan",
    "LaunchRequest",
    "MatchRequest",
    "MatchResult",
    "OpenStackAdapter",
    "ResourceProfile",
    "RunnerContext",
    "RunnerPlugin",
    "RunnerRegistry",
    "SchedulingRequest",
    "SlurmAdapter",
    "TaskDefinition",
    "WorkerAdvertisement",
    "WorkerRecord",
    "artifact_id",
    "assert_conformant",
    "normalize_result",
    "pack_directory",
    "run",
    "safe_extract",
    "task_digest",
    "validate_capability_payload",
    "validate_result_payload",
    "validate_runner_result_payload",
]
__version__ = "0.9.0"
