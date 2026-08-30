# Brunost Judge

Current release: `1.3.1` — local match execution, formal agent protocols, portable artifacts, worker credentials, scoped service authentication, secret-file loading, audit logging, rate limiting, signed callbacks, capability scheduling, provider adapters, deterministic game contracts, replay artifact results, and durable callback delivery.

Brunost Judge is the platform-independent judging layer for scorer-backed IOAI
and output-only tasks, plus versioned model, optimization, quiz, coding, interactive, and agent
runners. Model tasks use the dedicated `train_predict_v2` lifecycle. It is intentionally separate from the NOKI/Brunost education
platform: task authors can use the core and CLI directly, while an LMS or contest
platform integrates through the SDK/API boundary. The built-in runtime fails
closed for runner kinds that do not have an installed evaluator plugin.

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
export BRUNOST_JUDGE_API_TOKEN=local-development-only
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

judge = JudgeClient("http://127.0.0.1:8787", token="local-development-only")
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
onboarding, and [`docs/local-worker-smoke-test.md`](docs/local-worker-smoke-test.md)
for a reproducible local Judge-plus-worker test,
[`docs/production.md`](docs/production.md) for production controls, and
[`docs/rollout.md`](docs/rollout.md) for the supervised canary checklist.
See [`docs/ownership.md`](docs/ownership.md) for the boundary between the judge
and an LMS/platform.

Agent and game competitions use the versioned runner-plugin SDK documented in
[`docs/plugins.md`](docs/plugins.md). Participant bundles are content-addressed,
worker claims honor required capabilities, and custom runners are installed as
trusted evaluator-image extensions. See [`docs/agent-protocol.md`](docs/agent-protocol.md)
and the reference packages under `examples/agents` and `examples/games` for a
complete protocol-compatible match. Local game authors can exercise that full
path with `brunost match run` before publishing a task to the worker API.

For the generated application layer, framework adapters, and standalone/
embedded/hybrid integration modes, see [`docs/platform-kit.md`](docs/platform-kit.md).
Plugin authors can use the dependency-free conformance helpers in
`brunost_judge.conformance` to validate result and worker-capability payloads
in CI.
The API and provider model are documented in [`docs/api.md`](docs/api.md) and
[`docs/adapters-and-scheduling.md`](docs/adapters-and-scheduling.md).

## Documentation guide

Use the document that matches the role you are performing; the Judge keeps
execution concerns separate from contest and identity policy.

| If you are… | Start here | Then read |
| --- | --- | --- |
| Trying the Judge on one machine | [`docs/standalone.md`](docs/standalone.md) | [`docs/local-worker-smoke-test.md`](docs/local-worker-smoke-test.md) |
| Integrating an LMS, contest system, or submission service | [`docs/api.md`](docs/api.md) | [`docs/ownership.md`](docs/ownership.md) and [`docs/architecture.md`](docs/architecture.md) |
| Authoring a task | This README’s runner sections | [`docs/classic-tasks.md`](docs/classic-tasks.md), [`docs/model-tasks.md`](docs/model-tasks.md), [`docs/optimization-tasks.md`](docs/optimization-tasks.md), or [`docs/plugins.md`](docs/plugins.md) |
| Adding a CPU/GPU worker node | [`docs/node-onboarding.md`](docs/node-onboarding.md) | [`docs/adapters-and-scheduling.md`](docs/adapters-and-scheduling.md) |
| Operating a shared or official contest deployment | [`docs/production.md`](docs/production.md) | [`docs/rollout.md`](docs/rollout.md) and [`docs/architecture.md`](docs/architecture.md) |

The most useful production reading order is: architecture, API contract,
production profile, node onboarding, then the supervised rollout checklist.
The OpenAPI schema at `/openapi.json` is the authoritative field-level HTTP
reference for the version of the server you are running.

## Generic scorer contract

Generic scorer tasks receive these inputs (mounted read-only into a sealed sandbox by the worker):
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
- For legacy scorer-backed tasks, `score` is the **public** value — safe to surface live;
  the private value lives in `metrics["private"]`.
- Any error (missing file, bad shape, scorer exception) → `{"status": "failed", "score": 0.0,
  "failure_reason": "..."}` — the harness never crashes the sandbox.

An IOAI/output-only package should declare either `scorer/metrics.py` with
`scoring: scorer.metrics:evaluate`, or the supported legacy root `metrics.py`
with `scoring: metrics:evaluate` (older packages may omit the field and infer
it from their scorer location). The scorer must define
`evaluate(submission_path, assets_path)`. Generic scorer tasks are always
network-disabled. Set `resource_class` (for example `cpu` or `gpu`) in
`judge.yaml` when the task must run on a particular worker pool, and optionally
add a comma-separated `required_capabilities` value such as
`runtime:docker,gpu:true`. The task’s declared resource class and capabilities
are applied at registration and cannot be weakened by an evaluation request.
Feedback visibility belongs to the integrating platform, not a Judge manifest.

### Model and ML tasks

Model tasks use a separate v2 contract: `train()` writes a model artifact,
`predict()` writes predictions for a selected split, and `evaluator.py` scores
those predictions. The same lifecycle supports optional baselines and a hidden
post-competition leaderboard using new training and test data. See
[`docs/model-tasks.md`](docs/model-tasks.md) for the manifest, phase boundaries,
runtime, and complete submission contract.

### Deterministic coding tasks

`coding` task packages use the built-in classic runner. `icpc` remains a
legacy alias for existing packages. A minimal
manifest is flat and dependency-free:

```yaml
version: 1
kind: coding
runner: classic
language: cpp
runtime: python-3.13
time_limit_ms: 2000
memory_limit_mb: 512
output_limit_bytes: 1048576
network: disabled
scoring_mode: all_or_nothing # or percentage
```

The task contains matching `tests/**/*.in` and `.ans`/`.out` files. With
`all_or_nothing`, the submission receives full score only when every test
passes. With `percentage`, each solved test contributes an equal share of the
task score. Public copies of tests are optional convenience fixtures; every
test under `tests/` is evaluated.

The default checker compares whitespace-separated tokens. A task can provide
`checker.py` with `check(input_path, answer_path, output_path)` for custom
validation. Supported submission languages are Python, C, C++17, and Rust.
Compile errors, runtime errors, time limits, output limits, and partial scores
are returned as structured test metrics, including the scoring mode and the
number of passed tests.

For production, the Docker evaluator image installs compilers and bubblewrap;
private task assets stream into a root-only evaluator tmpfs and are never mounted
into the contestant process. Contestant/compiler processes run as dedicated UID
65533; runtimes that permit nested user namespaces also get bubblewrap isolation.
The local process runner remains a development mode only.

Production workers fail closed unless `BRUNOST_JUDGE_SANDBOX_MODE=docker` is
explicitly configured with a digest-pinned evaluator image. Artifact-backed
tasks and submissions are verified by SHA-256 before execution.

The package's runtime, scheduling labels, resource class, and network policy
are authoritative at registration; callers cannot replace them when creating
an evaluation. `brunost task validate` also rejects ambiguous answer keys,
orphan answer files, oversized test sets, and reference solutions outside
`private/`. See [`docs/classic-tasks.md`](docs/classic-tasks.md) for the full
coding package, checker, reference-solution, and CI contract.

`interactive` task packages use the same manifest and `tests/**/*.in` layout,
but provide an `interactor.py` with `interact(session, input_path)`. The
interactor exchanges newline-delimited UTF-8 messages through `session.send()`
and `session.receive()`, then returns `True`/`False` or a verdict/score object.

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
