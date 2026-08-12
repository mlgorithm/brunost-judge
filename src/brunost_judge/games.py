"""Trusted referee and deterministic match contracts.

Referees are trusted evaluator plugins. Contestant agents remain isolated by
the worker runtime; this module only coordinates the referee-facing contract.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol


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
