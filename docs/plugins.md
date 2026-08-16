# Runner plugins

Agent and game tasks use the versioned runner-plugin protocol. The control
plane stores the task and participant artifacts; the worker stages them into a
private bundle and invokes the selected evaluator sandbox.

## Task package

An agent or game task declares `runner: python` and contains `runner.py`:

```yaml
version: 1
kind: game
runner: python
network: disabled
```

```python
from pathlib import Path


def run(context: dict) -> dict:
    participants = context["participants"]
    assert all(Path(path).is_dir() for path in participants.values())
    return {
        "status": "completed",
        "score": 1.0,
        "scores": {"red": 1.0, "blue": 0.0},
        "replay": {"seed": context["seed"]},
        "metrics": {"rounds": 1},
    }
```

The context includes `protocol_version`, `execution_id`, `task_ref`,
`evaluation_kind` (`agent` or `match`), `task_path`, `submission_path`,
`participants`, `seats`, `seed`, and JSON metadata. Participant paths are
staged inside the evaluator submission mount. `participants` is keyed by agent
ID; `seats` preserves match order and supports the same agent occupying more
than one seat. A plugin must return a terminal result with a finite numeric
`score` and object `metrics`; match-specific `scores` and `replay` values are
retained under `metrics`.

## Registering participants

Participant artifacts should be uploaded and registered immutably:

```python
agent_bundle = judge.upload_artifact("agents/red")
judge.register_agent(
    agent_id="red",
    name="Red",
    artifact_id=agent_bundle["artifact_id"],
)
```

Game definitions reference a `game` task and match submissions reference the
registered agents. Agent and game definitions may declare
`required_capabilities`; the scheduler unions those requirements with the task
requirements and only compatible workers may claim the evaluation.

## Custom image plugins

The bundled `python-task-runner` loads the task's `runner.py`. An evaluator
image can install a trusted plugin module and set
`BRUNOST_JUDGE_RUNNER_PLUGIN_MODULE` to a module exposing
`register(registry)`. The module registers a `RunnerPlugin` with a name,
version, supported kinds, and `run(context)` method. Custom plugins are part of
the immutable evaluator image and are never loaded from an untrusted API
request.

Production workers must use the Docker/gVisor/Kata sandbox profile. The local
process runner is for development and plugin authoring only.
