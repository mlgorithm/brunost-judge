"""Local reference match execution for task and agent authors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from brunost_judge.games import AgentSeat
from grader.plugins import PythonTaskPlugin, RunnerContext


def run_local_match(
    task_path: str | Path,
    agents: tuple[AgentSeat, ...],
    *,
    seed: int = 0,
    match_id: str = "local-match",
    output_path: str | Path = "match-output",
) -> dict[str, Any]:
    """Run a trusted Python game runner against local agent bundles.

    This is intentionally a developer tool. Production submissions must use
    the worker's Docker/gVisor/Kata sandbox and immutable artifact staging.
    """

    task = Path(task_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not agents:
        raise ValueError("at least one --agent ID=PATH is required")
    if len({agent.agent_id for agent in agents}) != len(agents):
        raise ValueError("agent IDs must be unique")
    if len({agent.seat for agent in agents}) != len(agents):
        raise ValueError("agent seats must be unique")
    participants: dict[str, str] = {}
    seats: list[dict[str, Any]] = []
    for agent in sorted(agents, key=lambda item: item.seat):
        artifact = Path(agent.artifact_path).expanduser().resolve()
        if not artifact.is_dir():
            raise ValueError(f"agent artifact is not a directory: {artifact}")
        participants[agent.agent_id] = str(artifact)
        seats.append({"agent_id": agent.agent_id, "seat": agent.seat, "path": str(artifact), "metadata": dict(agent.metadata)})
    context = RunnerContext(
        execution_id=match_id,
        task_ref=task.name,
        evaluation_kind="match",
        task_path=str(task),
        submission_path=str(output),
        participants=participants,
        seats=tuple(seats),
        seed=seed,
        output_path=str(output),
        metadata={"local_match": True},
    )
    result = dict(PythonTaskPlugin().run(context))
    metrics = dict(result.get("metrics", {}))
    metrics.setdefault("local_match", {"match_id": match_id, "seed": seed, "agents": list(participants)})
    result["metrics"] = metrics
    return result


__all__ = ["run_local_match"]
