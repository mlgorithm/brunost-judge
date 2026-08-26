"""Trusted referee and deterministic match contracts.

Referees are trusted evaluator plugins. Contestant agents remain isolated by
the worker runtime; this module only coordinates the referee-facing contract.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from brunost_judge.agent_runtime import (
    AgentLimits,
    AgentRuntime,
    AgentRuntimeError,
    AgentSpec,
)


@dataclass(frozen=True)
class AgentSeat:
    agent_id: str
    artifact_path: str
    seat: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchRequest:
    game_id: str
    match_id: str
    agents: tuple[AgentSeat, ...]
    seed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    match_id: str
    status: str
    scores: dict[str, float]
    replay: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None


class Referee(Protocol):
    def run(self, request: MatchRequest, *, rng: random.Random) -> MatchResult: ...


class RuntimeReferee(Protocol):
    def run(self, request: MatchRequest, *, rng: random.Random, agents: AgentRuntime) -> MatchResult: ...


class GameRunner:
    """Validate match invariants and invoke a trusted deterministic referee."""

    def run(self, request: MatchRequest, referee: Referee, *, expected_seats: int) -> MatchResult:
        if len(request.agents) != expected_seats:
            return MatchResult(request.match_id, "failed", {}, failure_reason="agent count does not match game seats")
        if len({agent.seat for agent in request.agents}) != len(request.agents):
            return MatchResult(request.match_id, "failed", {}, failure_reason="duplicate agent seat")
        try:
            return referee.run(request, rng=random.Random(request.seed))
        except Exception as exc:  # noqa: BLE001 - evaluator failures become match results
            return MatchResult(request.match_id, "failed", {}, failure_reason=f"referee failure: {type(exc).__name__}: {exc}"[:2000])


class AgentGameRunner:
    """Launch one bounded agent process per seat before invoking a referee."""

    def run(
        self,
        request: MatchRequest,
        referee: RuntimeReferee,
        *,
        expected_seats: int,
        limits: AgentLimits | None = None,
    ) -> MatchResult:
        if len(request.agents) != expected_seats:
            return MatchResult(request.match_id, "failed", {}, failure_reason="agent count does not match game seats")
        if len({agent.seat for agent in request.agents}) != len(request.agents):
            return MatchResult(request.match_id, "failed", {}, failure_reason="duplicate agent seat")
        try:
            specs = tuple(
                AgentSpec(
                    agent_id=agent.agent_id,
                    seat=agent.seat,
                    artifact_path=agent.artifact_path,
                    command=_command_from_metadata(agent.metadata),
                    seed=request.seed,
                    metadata=agent.metadata,
                )
                for agent in request.agents
            )
            with AgentRuntime(specs, limits=limits, seed=request.seed) as agents:
                result = referee.run(request, rng=random.Random(request.seed), agents=agents)
                metrics = dict(result.metrics)
                metrics.setdefault("agent_runtime", agents.metrics())
                return MatchResult(
                    result.match_id,
                    result.status,
                    dict(result.scores),
                    replay=dict(result.replay),
                    metrics=metrics,
                    failure_reason=result.failure_reason,
                )
        except AgentRuntimeError as exc:
            return MatchResult(request.match_id, "failed", {}, failure_reason=f"agent runtime failure: {exc}"[:2000])
        except Exception as exc:  # noqa: BLE001 - referee failures become match results
            return MatchResult(request.match_id, "failed", {}, failure_reason=f"referee failure: {type(exc).__name__}: {exc}"[:2000])


def _command_from_metadata(metadata: dict[str, Any]) -> tuple[str, ...] | None:
    command = metadata.get("command")
    if isinstance(command, str):
        import shlex

        return tuple(shlex.split(command))
    if isinstance(command, (list, tuple)) and all(isinstance(item, str) and item for item in command):
        return tuple(command)
    return None
