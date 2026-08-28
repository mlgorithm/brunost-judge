# Supervised canary checklist

This checklist is the remaining operational step before an official contest.
It is intentionally separate from the open-source judge code so each country
can run it in its own infrastructure.

## Before traffic

- Pin the API and worker images by digest.
- Set unique random values for `BRUNOST_JUDGE_API_TOKEN`,
  `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET`, and the database password.
- Configure `BRUNOST_TASK_ROOT` and `BRUNOST_SUBMISSION_ROOT` to mounted,
  read-only/read-write paths respectively.
- Confirm the task package hash and scorer version in the contest manifest.
- Confirm PostgreSQL backups and a tested restore in a separate environment.
- Record the API, worker, evaluator-image, task-bundle, and scorer digests in
  the change record. A canary result is only meaningful when these inputs are
  known and can be reproduced.
- Confirm the callback receiver has an event-ID unique constraint or equivalent
  durable de-duplication before it receives live traffic.

## Smoke test

1. Run `scripts/canary.sh` against one deterministic coding/CPU task. The command uploads the
   task and submission as immutable artifacts, registers the task by digest,
   submits twice with one idempotency key, and waits for a completed result.
2. Confirm the canary output reports `immutable_task_artifact`,
   `immutable_submission_artifact`, `idempotency`, and `completed` as `true`.
3. If `BRUNOST_JUDGE_CANARY_CALLBACK_URL` is configured, confirm the callback
   receiver verifies the signature with the event ID and records one result.
4. Replay the exact callback immediately and after five minutes; the receiver
   must acknowledge an immediate duplicate without applying it twice and reject
   the stale duplicate. The signed event ID is the durable deduplication key;
   timestamp expiry is an additional freshness check.
5. Stop the worker during a lease, wait for expiry, and confirm another worker
   reclaims the execution.
6. Confirm `/v1/stats` returns zero queued/running work after the callback.

Capture the terminal execution JSON, callback receiver log, `/v1/stats`, and
image/task digests as the canary evidence. If any check fails, keep traffic on
the existing path, drain the affected worker pool, and preserve those artifacts
for diagnosis instead of re-running the same canary without a change.

The same workflow is covered in CI by
`tests/test_distributed_canary.py`. It starts a real HTTP API, enrolls a remote
worker, downloads artifacts over the worker API, verifies a signed callback,
and reclaims a deliberately expired lease with a second worker.

## Canary and rollback

Route a small cohort through the standalone gateway while the platform's current
worker remains available. Watch queue age, execution latency, failure rate,
callback retries, and host resource usage. Roll back by disabling the external
gateway flag and draining the standalone queues; no platform tables are shared.

Do not promote to an official contest until sandbox escape, network egress,
resource exhaustion, database restore, and worker-loss tests are documented by
the hosting operator.

## Minimum release record

For each promotion, retain:

- the commit, container image digests, evaluator-image digests, and task
  artifact digests;
- the canary execution ID and its terminal JSON response;
- the callback receiver’s de-duplication/replay evidence;
- the worker IDs, queues, resource classes, and capabilities used;
- the most recent successful backup and restore-drill timestamp; and
- the named operator who can drain workers and roll back the gateway.

This record turns a successful smoke test into a reproducible release decision,
and gives the next operator enough context to investigate a later regression.
