"""Public, platform-independent Brunost Judge API.

The compatibility ``grader`` package remains importable for task packages that
were authored before the standalone repository was extracted.
"""

__version__ = "1.3.1"

from brunost_judge.adapters import (
    DockerAdapter,
    KubernetesAdapter,
    LaunchPlan,
    LaunchRequest,
    OpenStackAdapter,
    SlurmAdapter,
)
from brunost_judge.agent_protocol import (
    DEFAULT_MAX_MESSAGE_BYTES,
    MESSAGE_TYPES,
    PROTOCOL_VERSION,
    ProtocolDirection,
    ProtocolValidationError,
    decode_message,
    encode_message,
    protocol_spec,
    validate_message,
)
from brunost_judge.agent_runtime import (
    AgentCrashed,
    AgentLaunchError,
    AgentLimits,
    AgentProtocolError,
    AgentRuntime,
    AgentRuntimeError,
    AgentSeatMetrics,
    AgentSpec,
    AgentTimeout,
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
from brunost_judge.games import (
    AgentGameRunner,
    AgentSeat,
    GameRunner,
    MatchRequest,
    MatchResult,
)
from brunost_judge.local_match import run_local_match
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
    "DEFAULT_MAX_MESSAGE_BYTES",
    "MESSAGE_TYPES",
    "PLUGIN_KINDS",
    "PLUGIN_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "RESULT_SCHEMA_VERSION",
    "AgentCrashed",
    "AgentDefinition",
    "AgentGameRunner",
    "AgentLaunchError",
    "AgentLimits",
    "AgentProtocolError",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentSeat",
    "AgentSeatMetrics",
    "AgentSpec",
    "AgentTimeout",
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
    "ProtocolDirection",
    "ProtocolValidationError",
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
    "decode_message",
    "encode_message",
    "normalize_result",
    "pack_directory",
    "protocol_spec",
    "run",
    "run_local_match",
    "safe_extract",
    "task_digest",
    "validate_capability_payload",
    "validate_message",
    "validate_result_payload",
    "validate_runner_result_payload",
]
__version__ = "1.2.0"
