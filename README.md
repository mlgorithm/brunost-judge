# Brunost Judge

Brunost Judge is the platform-independent judging layer for ICPC, IOI, IOAI,
and agent tasks. It is intentionally separate from the NOKI/Brunost education
platform: task authors can use the core and CLI directly, while an LMS or contest
platform integrates through the SDK/API boundary.

This first public extraction is deliberately small. It contains the scorer core,
task package validator, and local CLI. The server, worker fleet, and reference
console will be added behind the same contracts rather than copied from a
platform-specific backend.

## Quick start

```bash
python -m pip install -e '.[dev]'
brunost task new ioai tasks/example
brunost task validate tasks/example
brunost run tasks/example --submission ./submission
```

The generated task can be run locally without a database, Redis, cloud account,
or Brunost platform. Official workers mount the same task package into a sealed
sandbox.

## The contract

**Inputs** (mounted read-only into a sealed sandbox by the worker):
- `SUBMISSION_PATH` — directory with the contestant's uploaded file(s) (e.g. `submission.npz`, `submission.csv`).
- `ASSETS_PATH` — directory with the task's **`metrics.py`** + its **hidden labels** (answer key).

**The task author writes `metrics.py`** exposing one function:

```python
def evaluate(submission_path: str, assets_path: str) -> dict | float:
    # load the contestant's file from submission_path,
    # load hidden labels from assets_path, compute, and return a score.
    ...
```

`evaluate` may return any of these shapes — the harness normalizes them:
- a plain number → a single public score;
- `{"public": 0.98, "private": 0.97, "public_detail": {...}}` → a flat public/private split;
- `{"score": {"public_a": 0.98, "private_b": 0.97, "public_detail": {...}}}` → the IOAI shape.

**Output** (`results.json` the worker reads):
```json
{"status": "completed", "score": 0.98,
 "metrics": {"public": 0.98, "private": 0.97, "public_detail": {...}, "private_detail": {...}}}
```
- `score` is the **public** value — safe to surface live; the **private** value lives only
  in `metrics["private"]`, which the platform gates behind leaderboard freeze/reveal.
- Any error (missing file, bad shape, scorer exception) → `{"status": "failed", "score": 0.0,
  "failure_reason": "..."}` — the harness never crashes the sandbox.

## Usage in the sandbox

The public Python API is:

```python
from brunost_judge import normalize_result, run
```

The legacy `grader` import remains available for existing task packages.

The worker runs `python evaluate.py` inside the sealed container; `evaluate.py` reads the
env paths, calls `harness.run`, and writes `results.json` to `RESULT_PATH`.

## Repository boundary

`brunost-judge` owns reusable judging contracts and execution-facing tooling.
The main Brunost platform owns users, contests, official leaderboard policy,
appeals, medals, and country operations. The two systems communicate through a
versioned API and signed result callbacks; they do not share database tables.
