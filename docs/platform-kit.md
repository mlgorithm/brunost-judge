# Platform Kit integration

The judge can be used directly by an existing LMS, or together with the
separate [`brunost-platform-kit`](https://github.com/mlgorithm/brunost-platform-kit)
application layer.

## Judge-only integration

Use the versioned API or the dependency-free Python SDK:

```python
from brunost_judge.sdk import JudgeClient

judge = JudgeClient("http://127.0.0.1:8787")
evaluation = judge.submit_evaluation(
    task_ref="coding/radar-v1",
    submission_path="/var/lib/submissions/attempt-1",
    idempotency_key="student-42-attempt-1",
    evaluation_kind="batch",
)
```

The platform keeps the user and contest record. It stores the returned
`evaluation_id`, consumes the signed callback, and decides how the result is
shown on its leaderboard.

## Agent and game definitions

Agents and games are durable judge-side definitions:

```python
agent = judge.upload_artifact("agents/baseline")
judge.register_agent(
    agent_id="baseline",
    name="Baseline",
    artifact_id=agent["artifact_id"],
)
judge.register_game(
    game_id="connect-four-v1",
    name="Connect Four",
    task_ref="games/connect-four-v1",
    seats=2,
)
match = judge.submit_match(
    "connect-four-v1",
    agent_refs=["baseline", "baseline"],
    submission_path="/var/lib/matches/match-1",
    idempotency_key="match-1",
    seed=17,
)
```

Agent and match evaluations are executable when their task package contains a
versioned `runner.py` plugin. The worker stages the registered participant
artifacts, runs the plugin in the configured evaluator sandbox, and persists
the canonical result plus match scores/replay metadata. See
[`plugins.md`](plugins.md) for the plugin contract and custom evaluator-image
extension point.

## Platform Kit

Install the separate kit and generate an application:

```bash
python -m pip install -e ../brunost-platform-kit
brunost-platform init my-country --template python-fastapi
```

Templates are available for Python/FastAPI, Node.js/TypeScript/Fastify, and a
framework-neutral skeleton. Identity, email, contests, and leaderboard storage
remain replaceable adapters.
