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
returns the original evaluation instead of creating duplicate work. Evaluation
responses expose both `evaluation_id` (canonical) and `execution_id` (legacy
compatibility).

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
[`model-tasks.md`](model-tasks.md). `icpc` tasks use the classic batch runner
and return structured compile, test, scoring,
verdict, time, and output metrics. `interactive` tasks use the
line-oriented interactor runner. `agent` and `game` tasks use the versioned
runner-plugin contract; registered participant artifacts are staged into the
evaluator sandbox and game scores/replays are retained in result metrics.
Trusted game runners can use the bundled dependency-free `AgentRuntime` to
launch one bounded JSONL process per seat with deterministic turn ordering;
see the [runner-plugin protocol](plugins.md#agent-protocol) for the wire
contract and resource limits.

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

## Worker lifecycle

Workers register their capabilities, send heartbeats, and can be drained before
maintenance:

```text
POST /v1/workers/register
POST /v1/workers/{worker_id}/heartbeat?status=ready
POST /v1/workers/{worker_id}/drain?draining=true
POST /v1/workers/{worker_id}/claim
POST /v1/workers/{worker_id}/finish
GET /v1/workers/{worker_id}/executions/{execution_id}/cancel-requested
```

The scheduler must never infer a GPU or runtime capability from a hostname. A
worker advertises capabilities such as `gpu:true`, `runtime:kubernetes`, and
`region:nordic`; the control plane matches those labels to task requirements.

## Compatibility policy

- Additive fields are allowed in a minor release.
- Existing fields and endpoints remain supported for at least one major cycle.
- Breaking changes require a new `/v2` namespace or an explicit migration.
- Third-party workers and evaluator plugins should run the conformance helpers
  in `brunost_judge.conformance` in CI.
