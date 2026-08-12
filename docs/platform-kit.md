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
    task_ref="ioai/radar-v1",
    submission_path="/var/lib/submissions/attempt-1",
    idempotency_key="student-42-attempt-1",
    evaluation_kind="agent",
)
```

The platform keeps the user and contest record. It stores the returned
`evaluation_id`, consumes the signed callback, and decides how the result is
shown on its leaderboard.

## Agent and game definitions

Agents and games are durable judge-side definitions:

```python
judge.register_agent(agent_id="baseline", name="Baseline")
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

The reference worker still uses the task/evaluator seam for execution. A game
runner can consume the same definitions and metadata without changing the
public API.

## Platform Kit

Install the separate kit and generate an application:

```bash
python -m pip install -e ../brunost-platform-kit
brunost-platform init my-country --template python-fastapi
```

Templates are available for Python/FastAPI, Node.js/TypeScript/Fastify, and a
framework-neutral skeleton. Identity, email, contests, and leaderboard storage
remain replaceable adapters.
