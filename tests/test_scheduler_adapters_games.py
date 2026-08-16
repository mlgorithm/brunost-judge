from pathlib import Path

from brunost_judge.adapters import KubernetesAdapter, LaunchRequest, SlurmAdapter
from brunost_judge.agent_runtime import AgentLimits
from brunost_judge.games import (
    AgentGameRunner,
    AgentSeat,
    GameRunner,
    MatchRequest,
    MatchResult,
)
from brunost_judge.scheduler import (
    CapabilityScheduler,
    SchedulingRequest,
    WorkerAdvertisement,
)


def test_scheduler_matches_capabilities_and_prefers_region():
    scheduler = CapabilityScheduler()
    scheduler.register(WorkerAdvertisement("cpu-1", frozenset({"runtime:docker"}), region="west"))
    scheduler.register(WorkerAdvertisement("gpu-1", frozenset({"runtime:docker", "gpu:true"}), resource_classes=frozenset({"gpu"}), region="east"))
    selected = scheduler.choose(SchedulingRequest(resource_class="gpu", required_capabilities=frozenset({"gpu:true"}), preferred_region="east"))
    assert selected is not None and selected.worker_id == "gpu-1"


def test_provider_adapters_return_normalized_launch_plans():
    request = LaunchRequest("eval-1", "runtime:ioai", ("python", "evaluate.py"), gpu_count=1)
    kubernetes = KubernetesAdapter(namespace="contest")
    slurm = SlurmAdapter(partition="gpu")
    assert kubernetes.plan(request).payload["namespace"] == "contest"
    assert "--partition" in slurm.plan(request).payload["argv"]


def test_game_runner_enforces_seats_and_deterministic_referee():
    class Referee:
        def run(self, request, *, rng):
            return MatchResult(request.match_id, "completed", {request.agents[0].agent_id: rng.random()})

    request = MatchRequest("game-1", "match-1", (AgentSeat("a", "/a", 0), AgentSeat("b", "/b", 1)), seed=7)
    first = GameRunner().run(request, Referee(), expected_seats=2)
    second = GameRunner().run(request, Referee(), expected_seats=2)
    assert first.scores == second.scores
    failed = GameRunner().run(MatchRequest("game-1", "match-2", request.agents[:1]), Referee(), expected_seats=2)
    assert failed.status == "failed"


def test_agent_game_runner_orchestrates_isolated_stdio_agents(tmp_path: Path):
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "agent.py").write_text(
        """import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message[\"type\"] == \"init\":
        print(json.dumps({\"type\": \"ready\"}), flush=True)
    elif message[\"type\"] == \"turn\":
        print(json.dumps({\"type\": \"action\", \"action\": message[\"seat\"]}), flush=True)
    elif message[\"type\"] == \"shutdown\":
        break
""",
        encoding="utf-8",
    )

    class Referee:
        def run(self, request, *, rng, agents):
            actions = agents.step({"round": 1})
            return MatchResult(request.match_id, "completed", {"red": float(actions[0]), "blue": float(actions[1])})

    request = MatchRequest(
        "game-1",
        "match-runtime",
        (AgentSeat("red", str(agent), 0), AgentSeat("blue", str(agent), 1)),
        seed=17,
    )
    result = AgentGameRunner().run(
        request,
        Referee(),
        expected_seats=2,
        limits=AgentLimits(startup_timeout_seconds=1, turn_timeout_seconds=1, total_timeout_seconds=3),
    )

    assert result.status == "completed"
    assert result.scores == {"red": 0.0, "blue": 1.0}
