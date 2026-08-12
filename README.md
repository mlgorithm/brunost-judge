# Brunost Judge

Current release: `0.8.0` — zero-code node enrollment, portable artifacts, worker credentials, capability scheduling, provider
adapters, and deterministic game contracts.

Brunost Judge is the platform-independent judging layer for ICPC, IOI, IOAI,
and agent tasks. It is intentionally separate from the NOKI/Brunost education
platform: task authors can use the core and CLI directly, while an LMS or contest
platform integrates through the SDK/API boundary.

The distribution includes the scorer core, task package validator, local CLI,
SQLite development control plane, optional PostgreSQL control plane, HTTP API,
SDK, queue-aware worker, signed callbacks, operator console, hardened
Docker/gVisor overlay, backup/restore drills, and Docker Compose deployment.
High-assurance runtime availability remains a host certification step.

## Quick start

```bash
python -m pip install -e '.[dev]'
brunost task new ioai tasks/example
brunost task validate tasks/example
brunost run tasks/example --submission ./submission

# standalone API + worker
brunost server
brunost worker
```

Workers automatically register and heartbeat their queues, resource classes,
capabilities, and region. Advertise deployment-specific capabilities with
`brunost worker --capability gpu:true --capability runtime:kubernetes --region nordic`.

Country deployments can avoid shared filesystem mounts by uploading
content-addressed task/submission bundles with `brunost artifact upload`; remote
workers download and verify them before execution.

For a distributed control plane, install the production extra and configure an
S3-compatible artifact store on every API and worker process:

```bash
python -m pip install -e '.[production]'
export BRUNOST_JUDGE_ARTIFACT_BACKEND=s3
export BRUNOST_JUDGE_ARTIFACT_BUCKET=brunost-artifacts
export BRUNOST_JUDGE_ARTIFACT_ENDPOINT=https://s3.example.org
```

Artifact object keys are content-addressed and verified on upload and
download. Filesystem storage remains the default for local single-node use.

The generated task can be run locally without a database, Redis, cloud account,
or Brunost platform. Official workers mount the same task package into a sealed
sandbox.

## Standalone API

Install the server extra and start the reference control plane:

```bash
python -m pip install -e '.[server]'
brunost server
brunost worker

# PostgreSQL and production HTTP dependencies
python -m pip install -e '.[production]'
```

Then register and submit through the SDK:

```python
from brunost_judge.sdk import JudgeClient

judge = JudgeClient("http://127.0.0.1:8787")
judge.register_task(task_ref="demo/v1", path="./tasks/demo")
execution = judge.submit(
    task_ref="demo/v1",
    submission_path="./submissions/run-1",
    idempotency_key="student-1-demo-attempt-1",
)
```

The API is deliberately execution-oriented. A consuming LMS owns users,
contest rules, official leaderboard visibility, and appeals. The standalone
console is an operator health/API surface; it is not a replacement LMS.

Docker Compose provides the same flow for a small country or classroom:

```bash
mkdir -p local-submissions
docker compose up --build
```

See [`docs/standalone.md`](docs/standalone.md) for the country/operator flow,
[`docs/node-onboarding.md`](docs/node-onboarding.md) for zero-code three-node
onboarding,
[`docs/production.md`](docs/production.md) for production controls, and
[`docs/rollout.md`](docs/rollout.md) for the supervised canary checklist.
See [`docs/ownership.md`](docs/ownership.md) for the boundary between the judge
and an LMS/platform.

For the generated application layer, framework adapters, and standalone/
embedded/hybrid integration modes, see [`docs/platform-kit.md`](docs/platform-kit.md).
Plugin authors can use the dependency-free conformance helpers in
`brunost_judge.conformance` to validate result and worker-capability payloads
in CI.
The API and provider model are documented in [`docs/api.md`](docs/api.md) and
[`docs/adapters-and-scheduling.md`](docs/adapters-and-scheduling.md).

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
