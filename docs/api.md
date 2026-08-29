# Brunost Judge API

The HTTP API is versioned under `/v1`. The FastAPI service publishes the full
machine-readable schema at `/openapi.json` and interactive documentation at
`/docs`.

## Resource groups

| Group | Endpoints | Owner |
| --- | --- | --- |
| Tasks | `/v1/tasks`, `/v1/task-definitions` | Judge/task author |
| Evaluations | `/v1/evaluations`, `/v1/executions` | Integrating platform |
| Agents | `/v1/agents` | Judge/task author |
| Games | `/v1/games`, `/v1/games/{id}/matches` | Judge/task author |
| Workers | `/v1/workers` | Judge operator |
| Nodes | `/v1/nodes/enrollment-tokens`, `/v1/nodes/enroll` | Judge operator/node |
| Artifacts | `/v1/artifacts/{artifact_id}`, worker download endpoint | Judge/operator/worker |
| Callbacks | Signed callback URLs on evaluation requests | Integrating platform |
| Authentication/audit | `/v1/auth/*`, `/v1/audit` | Judge operator/service integration |

All mutating evaluation requests require an idempotency key. A repeated key
with the same request returns the original evaluation instead of creating
duplicate work; reusing it for different task, submission, callback, or queue
parameters returns `409 Conflict`. Evaluation responses expose both
`evaluation_id` (canonical) and `execution_id` (legacy compatibility).

`evaluation_kind` must match the registered task: `batch` for ordinary batch
tasks, `interactive` for interactive tasks, `agent` for agent tasks, and
`match` for game tasks. `agent_refs` are valid only for agent/match evaluations,
and games can reference only `kind: game` tasks.

Result payloads use stable `result_version: 1` semantics: terminal `status`,
numeric or null `score`, optional per-seat `scores`, optional `winner`, object
`metrics`, immutable result `artifacts`, optional `failure_reason`, and the
compatibility pair `evaluation_id`/`execution_id`. Workers reject malformed or
non-finite sandbox results before they are persisted. A plugin can declare
relative files such as a replay under `output_path`; workers upload them as
content-addressed bundles and expose them through `GET /v1/artifacts/{artifact_id}`.
Set `timeout_seconds` on an evaluation for a per-run wall-clock limit. A
running worker checks cancellation at the sandbox boundary and records a
finished-after-cancel run as `canceled`.

`ioai` and `output-only` tasks use the generic scorer contract. `model` tasks
use the v2 train/model/predict contract documented in
[`model-tasks.md`](model-tasks.md). `optimization` tasks use the trusted
feasibility/objective evaluator documented in
[`optimization-tasks.md`](optimization-tasks.md). `coding` tasks use the classic batch runner
and return structured compile, test, scoring,
verdict, time, and output metrics. `interactive` tasks use the
line-oriented interactor runner. `agent` and `game` tasks use the versioned
runner-plugin contract; registered participant artifacts are staged into the
evaluator sandbox and game scores/replays are retained in result metrics.
Trusted game runners can use the bundled dependency-free `AgentRuntime` to
launch one bounded JSONL process per seat with deterministic turn ordering;
see the [runner-plugin protocol](plugins.md#agent-protocol) for the wire
contract and resource limits.

`icpc` remains a legacy alias for existing coding task packages; integrations
should register new deterministic programming tasks as `coding`.

Premium Lab runtimes such as Pyodide, C/C++ WASM, and CheerpX are browser-only.
Judge rejects them as task runtimes; an official browser-originated submission
is instead re-executed with its registered contest task runtime.

For IOAI/output-only tasks, the registered package is authoritative for
`runtime`, `scoring`, `resource_class`, and `required_capabilities`.
When declared, `scoring` must name the packaged scorer
(`scorer.metrics:evaluate`) or the legacy root scorer (`metrics:evaluate`);
older packages may infer this from their scorer location. Every scorer must define
`evaluate(submission_path, assets_path)`. Generic scorer tasks must declare
`network: disabled`. A task-level `resource_class` overrides the evaluation
request’s resource class, and declared capabilities are combined with any
operator-supplied capabilities before worker selection. Feedback/leaderboard
visibility is platform policy and is not a Judge task-manifest field.

For `coding` tasks, the package likewise owns `runtime`, `network`,
`resource_class`, and `required_capabilities`. Registration records the classic
runner evaluator plus the derived whole-evaluation timeout, which includes
compilation and every private test; a request may shorten that deadline but
cannot extend it. Classic package validation requires one answer key per input
(`.ans` or `.out`), rejects stale/orphan answers, and requires a generated
reference solution to remain under `private/`. See
[`classic-tasks.md`](classic-tasks.md) for the manifest and trusted-checker
contract.

## Common integration flow

For a distributed deployment, use artifacts rather than `path` or
`submission_path`. Paths are only useful when the API can safely see the local
directory; artifacts are immutable and work across separate API and worker
hosts.

```bash
export BRUNOST_JUDGE_URL=https://judge.example
export BRUNOST_JUDGE_API_TOKEN='replace-with-service-token'

# Each command prints JSON containing artifact_id. Copy those values into the
# following requests, or use the SDK's upload_artifact()/submit_directory().
brunost artifact upload ./tasks/sum --url "$BRUNOST_JUDGE_URL" --token "$BRUNOST_JUDGE_API_TOKEN"
brunost artifact upload ./submissions/alice-1 --url "$BRUNOST_JUDGE_URL" --token "$BRUNOST_JUDGE_API_TOKEN"

curl --fail-with-body -X POST "$BRUNOST_JUDGE_URL/v1/tasks" \
  -H "Authorization: Bearer $BRUNOST_JUDGE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task_ref":"sum/v1","artifact_id":"<task-artifact-id>","kind":"coding"}'

curl --fail-with-body -X POST "$BRUNOST_JUDGE_URL/v1/evaluations" \
  -H "Authorization: Bearer $BRUNOST_JUDGE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task_ref":"sum/v1","submission_artifact_id":"<submission-artifact-id>","idempotency_key":"platform-submission-123","queue":"default","resource_class":"cpu"}'
```

The `202 Accepted` response contains `execution_id` (also returned as the
compatibility alias `evaluation_id`). Poll it until its status is terminal:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $BRUNOST_JUDGE_API_TOKEN" \
  "$BRUNOST_JUDGE_URL/v1/executions/<execution-id>"
```

Provide exactly one of `submission_path` and `submission_artifact_id`. Task
registration has the equivalent `path`/`artifact_id` choice. The API snapshots
an accepted path immediately, but an integrating service should normally
upload the bundle itself and retain the returned artifact ID for auditability.

### Request, retry, and result rules

| Situation | Expected behaviour for an integration |
| --- | --- |
| Network failure before a submission response | Retry the identical request with the identical idempotency key. |
| Same idempotency key and same request | Receive the original execution; do not create another platform attempt. |
| Same idempotency key with changed inputs | Receive `409 Conflict`; create a new platform attempt and key. |
| `202 Accepted` | Poll `/v1/executions/{id}` or wait for the callback. Accepted does not mean evaluated. |
| `completed`, `failed`, or `canceled` | Terminal. Store the result against the platform’s attempt once. |
| `429 Too Many Requests` | Back off for the `Retry-After` interval and retry safely. |
| `422 Unprocessable Content` | Correct the request/task bundle; retries alone will not help. |
| `503 Service Unavailable` | Treat as transient only after checking deployment configuration, especially callback and secret settings. |

Terminal records include `score`, `metrics`, optional per-seat `scores`,
optional `winner`, and result `artifacts`. A canceled result has no score or
result artifacts. `metadata` returned on the execution includes judge-owned
provenance such as `task_digest`, `runtime_image`, evaluator, and `event_id`.
Workers cannot replace those fields in their finish request.

## Authentication

Set `BRUNOST_JUDGE_API_TOKEN` and send `Authorization: Bearer <token>`. The
control-plane API is closed when no token is configured. Secrets can instead be
mounted by a container/orchestrator and loaded with
`BRUNOST_JUDGE_API_TOKEN_FILE=/run/secrets/brunost-admin-token`; when both forms
are present they must match. The health endpoint remains available without a
token. For a loopback-only development server, anonymous mode must be explicitly
enabled with `BRUNOST_JUDGE_ALLOW_ANONYMOUS_API=true`.

The Premium platform should use a scoped service credential rather than the
global admin token. An operator creates one with
`POST /v1/auth/service-credentials`; the raw token is returned once and only a
hash is stored. The default Premium scopes are `judge:read` and `judge:write`.
`judge:admin` is reserved for operator automation. Revoke a credential with
`POST /v1/auth/service-credentials/{credential_id}/revoke`. End-user login,
sessions, organizations, and contest roles remain Premium responsibilities;
the judge only authenticates service and worker credentials.

Admin tokens can be rotated without restarting the API when
`BRUNOST_JUDGE_API_TOKEN_FILE` is configured:

```bash
brunost auth rotate-admin-token --output /run/secrets/brunost-admin-token --force
```

The HTTP endpoint `POST /v1/auth/admin-token/rotate` performs the same atomic
file replacement and returns the new token once. Do not configure the legacy
`BRUNOST_JUDGE_API_TOKEN` environment value at the same time as this endpoint;
an immutable environment value would otherwise override the rotated file.

Mutating requests and authentication operations are written to `GET /v1/audit`
without request bodies or raw credentials. The built-in limiter is process
local: `BRUNOST_JUDGE_RATE_LIMIT_PER_MINUTE` defaults to 300 and
`BRUNOST_JUDGE_AUTH_RATE_LIMIT_PER_MINUTE` defaults to 30. Multi-replica
deployments should enforce the same limits at a shared ingress or Redis-backed
limiter.

Node enrollment is intentionally separate from the global API token. An
operator creates a short-lived token with `POST /v1/nodes/enrollment-tokens`.
The node consumes it once with `POST /v1/nodes/enroll` and receives a scoped
worker credential. Set `BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true` so heartbeats,
claims, and finishes require that credential. Revoke it with
`POST /v1/workers/{worker_id}/credential/revoke` if the node is lost.

Task and submission directories can be uploaded as deterministic gzip tar
bundles. The SHA-256 digest is the artifact ID, so workers can download and
verify inputs without a shared filesystem:

```bash
brunost artifact upload ./tasks/example --url https://judge.example
brunost artifact upload ./submissions/attempt-1 --url https://judge.example
```

Register a task with `artifact_id` and submit an evaluation with
`submission_artifact_id`. Artifacts reject symlinks, special files, traversal,
duplicate names, archive member/count/expansion limits, checksum mismatches, and
bundles larger than the configured limit.

## Callback receiver contract

Callbacks are a convenience for low-latency result delivery; polling remains
available as the recovery path. Treat every callback as at-least-once and keep
an application-level record keyed by `X-Brunost-Judge-Event-ID` (the same value
is in the result’s `event_id`).

When a callback signing secret is configured, workers send these headers:

| Header | Meaning |
| --- | --- |
| `X-Brunost-Judge-Timestamp` | Unix epoch seconds when the callback was signed |
| `X-Brunost-Judge-Event-ID` | Stable execution result event ID |
| `X-Brunost-Judge-Signature` | `sha256=` followed by HMAC-SHA256 of `timestamp.event_id.raw-body` |
| `Authorization` | Optional bearer token supplied on the evaluation request |

Verify the signature against the exact raw request body before decoding it,
reject timestamps outside the receiver’s replay window (the SDK default is
five minutes), and then de-duplicate the event ID in the same transaction that
applies the result. Do not use a callback’s arrival time or an execution ID
alone as the deduplication key. The SDK exposes the compatible verifier:

```python
from brunost_judge.sdk import JudgeClient

valid = JudgeClient.verify_callback(
    raw_body,
    secret=callback_secret,
    timestamp=request.headers["X-Brunost-Judge-Timestamp"],
    event_id=request.headers["X-Brunost-Judge-Event-ID"],
    signature=request.headers["X-Brunost-Judge-Signature"],
    require_event_id=True,
)
```

In production, callbacks require an allowlisted host and HTTPS. Workers do not
follow callback redirects. See [production.md](production.md) for the required
environment settings and [rollout.md](rollout.md) for replay testing.

## Worker lifecycle

Workers register their capabilities, send heartbeats, and can be drained before
maintenance:

```text
POST /v1/workers/register
POST /v1/workers/{worker_id}/heartbeat?status=ready
POST /v1/workers/{worker_id}/callbacks/{execution_id}/claim
POST /v1/workers/{worker_id}/callbacks/{execution_id}/ack
POST /v1/workers/{worker_id}/drain?draining=true
POST /v1/workers/{worker_id}/claim
POST /v1/workers/{worker_id}/finish
GET /v1/workers/{worker_id}/executions/{execution_id}/cancel-requested
POST /v1/workers/{worker_id}/executions/{execution_id}/lease
```

Workers renew an active execution lease while the sandbox is running. A lease
cannot be renewed after cancellation. Worker result metadata cannot replace the
control-plane metadata that binds the task digest, evaluator, runtime, and
callback event ID; integrations should retain the values returned in the claim.

When a node enrollment request includes `capabilities` or `resource_classes`,
the registered worker uses that reported subset (which must be within the
operator-approved enrollment token). Older nodes that omit an inventory retain
the token's approved inventory for compatibility.

`claim` returns `204 No Content` when no compatible execution is queued. A
claimed execution includes its task, artifact references, callback context, and
`lease_seconds`. A worker should renew at a fraction of that duration, check
the cancellation endpoint while evaluating, and finish only with a terminal
result. A finish or renewal after cancellation, lease expiry, or ownership
change returns `409 Conflict`; the worker must discard that stale result.

Terminal results with a callback URL are placed in the Judge callback outbox in
the same transaction as the result. Remote workers claim and acknowledge their
delivery lease after sending the callback; the control-plane callback
dispatcher retries anything left behind after a worker or network failure.

The scheduler must never infer a GPU or runtime capability from a hostname. A
worker advertises capabilities such as `gpu:true`, `runtime:kubernetes`, and
`region:nordic`; the control plane matches those labels to task requirements.

## Compatibility policy

- Additive fields are allowed in a minor release.
- Existing fields and endpoints remain supported for at least one major cycle.
- Breaking changes require a new `/v2` namespace or an explicit migration.
- Third-party workers and evaluator plugins should run the conformance helpers
  in `brunost_judge.conformance` in CI.
