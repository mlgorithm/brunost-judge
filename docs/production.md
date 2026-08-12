# Production profile

The standalone repository can start as a small control plane and scale out
without changing task packages or the SDK contract. Install
`brunost-judge[production]` to enable the PostgreSQL adapter.

Before an official contest, operators must provide:

- PostgreSQL (`BRUNOST_JUDGE_DATABASE_URL=postgresql://...`) instead of local
  SQLite. SQLite is suitable only for a single-node development installation;
- object storage for submissions, task packages, and bounded artifacts;
- isolated worker hosts using gVisor/Kata/Firecracker or an equivalent boundary;
- immutable, digest-pinned judge and runtime images;
- private worker queues (`--queue`) and resource pools (`--resource-class`);
- signed result callbacks using `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET` and the
  `X-Brunost-Judge-Timestamp` / `X-Brunost-Judge-Signature` headers. Production
  requires HTTPS callback URLs, an explicit `BRUNOST_JUDGE_CALLBACK_HOSTS`
  allowlist, and durable receiver-side deduplication of the signed event ID;
- one-time node enrollment with `BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true` and
  a separate scoped credential per worker;
- backups, monitoring, alerting, and a rehearsed restore/failover plan;
- a second failure domain for multi-country availability.

The base Compose profile uses PostgreSQL, read-only API/worker images,
queue/resource labels, and health-ordered startup. For an untrusted contest,
use the hardened overlay:

```bash
export BRUNOST_JUDGE_SANDBOX_IMAGE='ghcr.io/example/brunost-judge-runtime@sha256:<64-hex-digest>'
export BRUNOST_JUDGE_SANDBOX_RUNTIME=runsc   # or a certified Kata runtime
export BRUNOST_JUDGE_SANDBOX_SECCOMP=/etc/docker/seccomp/brunost-seccomp.json
docker compose -f docker-compose.yml -f docker-compose.production.yml up --build -d
```

The overlay makes the worker's Docker socket explicit, but every evaluator is
still launched with no network, read-only rootfs, dropped capabilities, quotas,
and the configured gVisor/Kata runtime. Build the sandbox image with
`Dockerfile.sandbox` on top of the pinned task runtime, then publish it by
digest. GPU workers should use a separately
certified runtime because gVisor GPU support is limited.

The reference artifact backend is a content-addressed filesystem. For a
multi-node country deployment, put that root on replicated S3/MinIO-compatible
object storage or deploy an equivalent artifact adapter; the worker protocol
does not require shared POSIX mounts.

## Deployment sequence

1. Create random values for the API token, callback signing secret, and database
   password; never use Compose development defaults.
2. Start the control plane and database, then register immutable task packages.
3. Start one CPU worker with `--queue default --resource-class cpu` and run the
   smoke test in `docs/rollout.md`.
4. Add GPU workers only when available; IOAI tasks remain supported on CPU.
5. Run a small canary, inspect `/v1/stats`, and verify callback signatures and
   replay rejection.
6. Promote additional worker pools after queue-drain, failure-injection, and
   restore tests pass.

For country-operated nodes, use the join workflow in
[`node-onboarding.md`](node-onboarding.md). Do not copy the global API token to
workers; workers should use the credential returned by `brunost node join`.

## Repeatable drills

- `scripts/canary.sh` registers the CPU task, submits twice with one idempotency
  key, and waits for a terminal result.
- `scripts/backup_postgres.sh` writes an atomic custom-format dump and checksum;
  copy the resulting directory to a different failure domain.
- `scripts/restore_drill.sh` restores that dump into a throwaway PostgreSQL
  container and checks the schema is non-empty.
- `scripts/failover_drill.sh` prints the supervised worker-loss sequence. Stop
  the first worker, wait for its lease, start a second worker, and confirm the
  same execution completes once.

The platform integration should switch traffic using its existing gateway and
outbox, not by sharing judge tables. Keep the current in-repo worker available
until the canary has passed and a rollback window has closed.
