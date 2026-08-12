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

## Smoke test

1. Register one IOAI CPU task.
2. Submit the same idempotency key twice; confirm one execution is created.
3. Consume it with a CPU worker and confirm the callback signature verifies.
4. Replay the callback after five minutes; the receiver must reject it.
5. Stop the worker during a lease, wait for expiry, and confirm another worker
   reclaims the execution.
6. Confirm `/v1/stats` returns zero queued/running work after the callback.

## Canary and rollback

Route a small cohort through the standalone gateway while the platform's current
worker remains available. Watch queue age, execution latency, failure rate,
callback retries, and host resource usage. Roll back by disabling the external
gateway flag and draining the standalone queues; no platform tables are shared.

Do not promote to an official contest until sandbox escape, network egress,
resource exhaustion, database restore, and worker-loss tests are documented by
the hosting operator.
