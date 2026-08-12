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
  `X-Brunost-Judge-Timestamp` / `X-Brunost-Judge-Signature` headers;
- backups, monitoring, alerting, and a rehearsed restore/failover plan;
- a second failure domain for multi-country availability.

The included Compose profile now uses PostgreSQL, read-only API/worker images,
queue/resource labels, and health-ordered startup. It is still a reference
installation: ordinary Docker is not a sufficient high-stakes isolation
boundary. Replace the worker service with a microVM-backed runner before
official IOAI/IOI use.

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

The platform integration should switch traffic using its existing gateway and
outbox, not by sharing judge tables. Keep the current in-repo worker available
until the canary has passed and a rollback window has closed.
