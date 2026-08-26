"""Small simultaneous-turn reference game for agent authors."""

from __future__ import annotations

import json
import random
from pathlib import Path

from grader.agent_runtime import AgentLimits, AgentRuntime

ROUNDS = 3


def run(context: dict) -> dict:
    seats = context.get("seats")
    if not isinstance(seats, list) or len(seats) != 2:
        return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": "exactly two seats are required"}
    scores = {str(seat["agent_id"]): 0.0 for seat in seats}
    replay: list[dict] = []
    random_source = random.Random(context.get("seed") if isinstance(context.get("seed"), int) else 0)
    runtime = AgentRuntime.from_context(
        context,
        limits=AgentLimits(turn_timeout_seconds=0.5, total_timeout_seconds=10, max_turns=ROUNDS),
    )
    with runtime:
        for round_number in range(1, ROUNDS + 1):
            target = random_source.randrange(10)
            actions = runtime.step(
                {"round": round_number, "choices": list(range(10))},
                turn=round_number,
                simultaneous=True,
            )
            choices: dict[int, int] = {}
            for seat, action in actions.items():
                if isinstance(action, bool) or not isinstance(action, int) or not 0 <= action <= 9:
                    return {
                        "status": "failed",
                        "score": 0.0,
                        "metrics": {"round": round_number, "runtime": runtime.metrics()},
                        "failure_reason": f"seat {seat} returned an invalid choice",
                    }
                choices[seat] = action
            distances = {seat: abs(choice - target) for seat, choice in choices.items()}
            best_distance = min(distances.values())
            winners = [seat for seat, distance in distances.items() if distance == best_distance]
            for seat in winners:
                agent_id = next(str(item["agent_id"]) for item in seats if item["seat"] == seat)
                scores[agent_id] += 1.0 / len(winners)
            replay.append({"round": round_number, "target": target, "actions": choices, "winners": winners})
        runtime_metrics = runtime.metrics()

    output = Path(context["output_path"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "replay.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in replay),
        encoding="utf-8",
    )
    highest = max(scores.values())
    winners = [agent_id for agent_id, score in scores.items() if score == highest]
    result = {
        "status": "completed",
        "score": highest / ROUNDS,
        "scores": {agent_id: score / ROUNDS for agent_id, score in scores.items()},
        "metrics": {"rounds": ROUNDS, "runtime": runtime_metrics},
        "artifacts": {"replay": {"path": "replay.jsonl", "kind": "replay"}},
    }
    if len(winners) == 1:
        result["winner"] = winners[0]
    return result
