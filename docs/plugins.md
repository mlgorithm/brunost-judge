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
`participants`, `seats`, `seed`, `output_path`, and JSON metadata. Participant
paths are staged inside the evaluator submission mount. `participants` is keyed
by agent ID; `seats` preserves match order and supports the same agent
occupying more than one seat. A plugin must return a terminal result with a
finite numeric `score` and object `metrics`. Match results may also return
finite per-seat `scores`, a `winner`, and inline `replay` metadata. Large
replays or other outputs should be written below `output_path` and declared as
relative files in `artifacts`, for example:

```python
Path(context["output_path"]).mkdir(parents=True, exist_ok=True)
Path(context["output_path"], "replay.jsonl").write_text("...", encoding="utf-8")
return {
    "status": "completed",
    "score": 1.0,
    "scores": {"red": 1.0, "blue": 0.0},
    "winner": "red",
    "artifacts": {
        "replay": {"path": "replay.jsonl", "kind": "replay"},
    },
}
```

Workers package declared files into immutable content-addressed artifacts and
return their references under the execution result's `artifacts` field. The
API serves those bundles from `GET /v1/artifacts/{artifact_id}`. Artifact files
are bounded by the worker's configured result-artifact size limit.

## Execution safety

An evaluation may set `timeout_seconds`; otherwise the worker derives a limit
from `time_limit_ms` or the sandbox default. A cancellation request is checked
before execution and immediately after the sandbox boundary, so a result that
finishes after cancellation is recorded as `canceled` rather than completed.
Docker workers additionally enforce the wall-clock limit while the evaluator
container is running.

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
