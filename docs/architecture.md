# Judge architecture and operational model

Brunost Judge is an execution control plane. It decides where an immutable
task/submission pair runs, records the resulting evidence, and emits a signed
result. It deliberately does not own users, contest eligibility, public
leaderboards, or notifications; see [ownership.md](ownership.md).

## System map

```text
                         control-plane credentials
Platform / LMS  ------------------------------------->  Judge API
      |                                                     |
      | immutable task and submission bundles              | PostgreSQL
      +----------------------------------------------------> | durable state
                                                            |
                                                            v
                                                     Artifact store
                                                            ^
                                                            | verified bundles/results
                                                            |
                    scoped worker credential                |
Worker node  <-----------------------------------------  claim + lease
      |                                                     |
      +-- isolated evaluator --> terminal result -----------+
                                                            |
Platform / LMS  <----------- signed, idempotent callback ---+
```

The API/control plane, database, artifact store, worker host, and callback
receiver are separate trust boundaries. A production deployment should avoid
placing untrusted evaluator work on the same host as the API, database, or
callback receiver.

## Components and durable data

| Component | Responsibility | Durable/authoritative data |
| --- | --- | --- |
| Integrating platform | Authenticates users, accepts submissions, applies contest policy and leaderboard visibility | User, contest, appeal, and leaderboard records |
| Judge API | Validates task/evaluation requests, schedules work, validates worker results | Task records, executions, worker credentials, audit events |
| Artifact store | Stores content-addressed tar bundles | Immutable task, submission, agent, and result-artifact bundles |
| Worker | Claims compatible work and runs a sandboxed evaluator | No source of record; it reports terminal results to the Judge |
| Callback receiver | Verifies, de-duplicates, and applies Judge results | The platform’s execution/event mapping |

The Judge snapshots task and submission directories before scheduling them.
The resulting SHA-256 artifact IDs can be used across hosts without shared
filesystems. A task record also retains its manifest digest, runtime, and
evaluator identity. Those values, plus a stable result `event_id`, are
control-plane provenance: worker-supplied completion metadata cannot overwrite
them.

## Evaluation lifecycle

```text
submit -> queued -> running -> completed
                    |  \-> failed
                    |  \-> canceled
cancel while queued -+-> canceled
lease expires ---------> queued (unless cancellation was requested)
```

- Submission uses an idempotency key. Retrying the *same* request returns the
  existing execution; using the same key for a different task, bundle,
  callback, queue, or other scheduling input returns `409 Conflict`.
- A compatible worker claims the queued execution and receives a time-limited
  lease. The worker renews that lease while the sandbox is running.
- Only the worker that owns a live `running` lease can finish the execution.
  A stale worker result is rejected with `409 Conflict` rather than replacing a
  newer result.
- Cancellation is authoritative. If it races with a completion, the final
  record is `canceled`; scores and result artifacts are not published.
- Only terminal executions (`completed`, `failed`, or `canceled`) are eligible
  for callback delivery.

The lease is a recovery mechanism, not a duplicate-execution guarantee for a
non-idempotent external evaluator. Task authors should make evaluator outputs
deterministic for a fixed task, submission, and seed. Integrators must treat
both polling and callbacks as at-least-once result delivery.

## Task and evaluation compatibility

The submitted `evaluation_kind` is validated against the registered task:

| Task kind | Required evaluation kind | Extra fields |
| --- | --- | --- |
| `ioai`, `output-only`, `coding` (or legacy `icpc`), `model`, `optimization`, `quiz` | `batch` | None |
| `interactive` | `interactive` | None |
| `agent` | `agent` | One or more `agent_refs` |
| `game` | `match` | `game_ref` and the game’s seats in `agent_refs` |

This validation prevents a generic task from being submitted to a game or
agent runner by accident. A game definition itself can only reference a
registered `kind: game` task.

## Deployment modes

| Mode | Store and artifacts | Intended use | Important limit |
| --- | --- | --- | --- |
| Local process | SQLite and filesystem artifacts | Task development and smoke tests | Never runs untrusted contest traffic |
| Reference Compose | PostgreSQL and a shared named artifact volume | Classroom/small-country integration test | Uses development defaults unless operators replace them |
| Distributed production | PostgreSQL, replicated S3/MinIO-compatible artifacts, isolated worker pools | Shared/official contest service | Requires host isolation, image certification, backups, and an operator rollout |

The reference Compose profile initializes `judge-artifacts`, a writable named
volume shared by the read-only API and worker containers. It is appropriate for
one Docker host. Use object storage instead of this volume once workers run on
different hosts.

## Security model and non-goals

The control plane protects several boundaries, but it is not itself a complete
contest security program.

- API and worker credentials are separate. A node enrollment token is short
  lived and single use; its worker credential is scoped to that worker.
- Worker placement is based on explicit queue, resource-class, and capability
  labels. Host names never imply GPU or runtime access.
- Production callbacks require HTTPS and an allowlisted host. The HMAC binds
  the raw payload to its timestamp and event ID; receivers must enforce replay
  and duplicate handling.
- Production evaluator images are digest pinned and executed without network,
  with a read-only root filesystem and a certified isolation runtime. The
  Docker/gVisor/Kata/Firecracker boundary remains an operator responsibility.
- The built-in HTTP rate limiter is process-local. Multi-replica deployments
  need a shared ingress or distributed rate limiter.

The Judge does not promise that a process sandbox is safe for hostile code,
that a task scorer is fair, or that a callback receiver is idempotent. Those
are explicit operator, task-author, and platform responsibilities.

## What to monitor

Before and during an event, monitor the state that describes the lifecycle:

| Signal | Healthy expectation | Investigate when |
| --- | --- | --- |
| `/v1/stats` queued/running counts | Queued work drains and running work stays bounded | Queue age grows, or work remains running beyond its timeout/lease |
| Worker list and heartbeat | Expected workers are `ready` and advertise the correct labels | A worker is offline, draining unexpectedly, or advertises an over-broad capability |
| Execution terminal mix | Completed work dominates expected failure modes | Failure/cancel rate changes after an image, task, or scheduler change |
| Callback delivery | Receiver records each `event_id` once | Retries accumulate or the receiver rejects valid signatures |
| Artifact/database backup drills | A recent backup restores in an isolated environment | Restore, digest verification, or least-privilege access fails |

Use the [production profile](production.md) for configuration controls and the
[rollout checklist](rollout.md) for the evidence required before promotion.
