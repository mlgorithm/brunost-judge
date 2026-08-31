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
- `BRUNOST_JUDGE_ENV=production`, explicit `BRUNOST_JUDGE_SANDBOX_MODE=docker`,
  and `BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS=true`; production refuses the
  in-process runner and unpinned evaluator images;
- a sandbox image built from `Dockerfile.sandbox` with the required compiler
  toolchains and `bubblewrap`; task bundles stream over evaluator stdin into a
  root-only tmpfs, while classic contestants run as dedicated UID 65533;
  bubblewrap remains available when the runtime supports nested user namespaces;
- private worker queues (`--queue`) and resource pools (`--resource-class`);
- signed result callbacks using `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET` and the
  `X-Brunost-Judge-Timestamp` / `X-Brunost-Judge-Signature` headers. Production
  requires HTTPS callback URLs, an explicit `BRUNOST_JUDGE_CALLBACK_HOSTS`
  allowlist, and durable receiver-side deduplication of the signed event ID.
  Mount the signing secret with
  `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET_FILE=/run/secrets/brunost-callback-secret`
  when possible, and set `BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS=true` to make
  callback requests fail closed if the secret is unavailable. Callback bearer
  tokens are defense in depth; if they are used, protect the database with
  encryption at rest because retries must retain the token to send it again.
  An isolated service mesh may set
  `BRUNOST_JUDGE_ALLOW_INTERNAL_HTTP_CALLBACKS=true` for an allowlisted
  internal hostname only; public callbacks remain HTTPS-only;
- a durable callback dispatcher (`brunost callback-dispatcher`) running on the
  control plane. Terminal results and callback outbox rows are committed in
  one database transaction, so worker loss after finishing an evaluation does
  not lose the integrating platform's notification;
- one-time node enrollment with `BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true` and
  a separate scoped credential per worker;
- backups, monitoring, alerting, and a rehearsed restore/failover plan;
- a second failure domain for multi-country availability.

The base Compose profile uses PostgreSQL, a durable callback dispatcher,
read-only API/worker images,
queue/resource labels, and health-ordered startup. For an untrusted contest,
use the hardened overlay:

```bash
export BRUNOST_JUDGE_SANDBOX_IMAGE='ghcr.io/example/brunost-judge-runtime@sha256:<64-hex-digest>'
export BRUNOST_JUDGE_SANDBOX_RUNTIME=runsc   # or a certified Kata runtime
export BRUNOST_JUDGE_SANDBOX_IMAGES='{"python-3.13":"ghcr.io/example/brunost-judge-runtime@sha256:<64-hex-digest>","python-3.13-ml-v1":"ghcr.io/example/brunost-judge-runtime-ml@sha256:<64-hex-digest>"}'
docker compose -f docker-compose.yml -f docker-compose.production.yml up --build -d
```

The base Compose profile creates `judge-artifacts`, a shared writable named
volume. It is initialized before the read-only API and worker containers start.
That makes the reference deployment runnable without weakening their
read-only root filesystems. It is still a single-host convenience volume: use
an S3/MinIO-compatible backend for a multi-host control plane and workers.

The overlay bind-mounts the checked-in, versioned
`src/brunost_judge/security/seccomp-v1.json` profile and passes it to every
evaluator. It is the maintained Docker Engine v28.5.2 default allowlist
profile, vendored as a release-controlled asset; task execution has no retained
Docker capabilities that would enable its conditional privileged calls. Keep
the default mount or point `BRUNOST_JUDGE_SECCOMP_HOST_PATH` only at a reviewed
copy of that exact profile. `brunost cluster init` writes the same file into
the generated worker bundle under `security/brunost-seccomp-v1.json`.

Every evaluator is also launched with no network, read-only rootfs, dropped
capabilities, quotas, and the configured gVisor/Kata runtime. The evaluator is
root only long enough to extract the root-only task tmpfs. Every native
compiler is unconditionally dropped to UID 65533 before it processes staged
submission/reference/baseline source, and every contestant runtime uses that
identity; private task assets are never readable by those processes. On
runtimes that permit it, bubblewrap adds a second mount namespace as defense
in depth. Build the sandbox image with
`Dockerfile.sandbox` on top of the pinned task runtime, then publish it by
digest. Build `Dockerfile.sandbox.ml` for the `python-3.13-ml-v1` entry and map
it with `BRUNOST_JUDGE_SANDBOX_IMAGES`; unmapped non-default runtimes fail closed.
GPU workers should use a separately certified runtime because gVisor GPU support
is limited.

The reference artifact backend is a content-addressed filesystem. For a
multi-node country deployment, put that root on replicated S3/MinIO-compatible
object storage or deploy an equivalent artifact adapter; the worker protocol
does not require shared POSIX mounts.

Every classic, interactive, plugin, and generic scorer manifest must declare
`version: 1`; model manifests use `version: 2` and the `train_predict_v2`
contract. Registration snapshots the task
as a content-addressed artifact and workers verify its digest again immediately
before evaluation. Generated Python bytecode is excluded from both snapshots
and digests so local caches cannot change the task identity.

## Deployment sequence

1. Create random values for the API token, callback signing secret, and database
   password; never use Compose development defaults. Prefer mounted secret
   files (`*_FILE`) for the API and callback secrets.
2. Start the control plane and database, then register immutable task packages.
3. Start one CPU worker with `--queue default --resource-class cpu` and run the
   smoke test in `docs/rollout.md`.
4. Add GPU workers only when available; IOAI tasks remain supported on CPU.
5. Run a small canary, inspect `/v1/stats`, and verify callback signatures and
   replay rejection.
6. Promote additional worker pools after queue-drain, failure-injection, and
   restore tests pass.

When Docker is available on the worker host, run the real sandbox check against
the published image before a canary:

```bash
BRUNOST_JUDGE_RUN_DOCKER_TESTS=true \
BRUNOST_JUDGE_SANDBOX_IMAGE='registry.example/judge@sha256:<digest>' \
pytest -q tests/test_docker_sandbox_integration.py
```

The integration test verifies a classic submission completes while a deliberate
attempt to open the task answer path is denied.

For country-operated nodes, use the join workflow in
[`node-onboarding.md`](node-onboarding.md). Do not copy the global API token to
workers; workers should use the credential returned by `brunost node join`.

## Repeatable drills

- `scripts/canary.sh` uploads immutable task/submission artifacts, registers the
  CPU task by digest, submits twice with one idempotency key, and waits for a
  terminal result. Set `BRUNOST_JUDGE_CANARY_CALLBACK_URL` to exercise an
  integrating platform's callback receiver as part of the run.
- `scripts/backup_postgres.sh` writes an atomic custom-format dump and checksum;
  copy the resulting directory to a different failure domain.
- `scripts/restore_drill.sh` restores that dump into a throwaway PostgreSQL
  container and checks the schema is non-empty.
- `scripts/failover_drill.sh` prints the supervised worker-loss sequence. Stop
  the first worker, wait for its lease, start a second worker, and confirm the
  same execution completes once.

The HTTP-level artifact, enrollment, callback-signature, idempotency, and lease
reclaim path is also exercised by `tests/test_distributed_canary.py` before a
release is promoted.

An integrating platform should switch traffic using its own gateway and outbox,
not by sharing Judge tables. Configure `BRUNOST_JUDGE_CALLBACK_HOSTS` with the
receiver hostname and use the same callback signing secret on both services.
Keep a known-good worker pool available until the canary has passed and a
rollback window has closed.

## Configuration safety gate

Before starting a shared deployment, verify these settings are intentional.

| Setting | Production expectation | Why it matters |
| --- | --- | --- |
| `BRUNOST_JUDGE_DATABASE_URL` | PostgreSQL URL, supplied as a secret | SQLite has no multi-host durability or coordination guarantee |
| `BRUNOST_JUDGE_API_TOKEN_FILE` | Mounted secret file; do not use Compose defaults | Protects every control-plane mutation |
| `BRUNOST_JUDGE_REQUIRE_IDEMPOTENCY_HEADER` | `true` in production | Binds retry identity to the HTTP request, not only its JSON body |
| `BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN` | `true` | Limits claims, leases, and finishes to enrolled workers |
| `BRUNOST_JUDGE_ARTIFACT_BACKEND` | Replicated object storage for multi-host deployments | Makes immutable task/submission bundles available to every worker |
| `BRUNOST_JUDGE_SANDBOX_MODE` | `docker` with a certified runtime | Prevents a production fallback to the development process runner |
| `BRUNOST_JUDGE_SANDBOX_IMAGES` | Every allowed runtime mapped to a digest-pinned image | Prevents unreviewed runtime image drift |
| `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET_FILE` | Mounted secret file, with signed callbacks required | Enables callback authenticity and replay protection |
| `BRUNOST_JUDGE_CALLBACK_HOSTS` | Explicit callback receiver allowlist | Prevents callback SSRF to arbitrary hosts |
| `BRUNOST_JUDGE_LEASE_SECONDS` | Longer than normal renewal latency, shorter than recovery tolerance | Balances worker-loss recovery against long-running evaluations |

HTTP clients should disable redirects for authenticated requests and enforce a
bounded response size. The SDK and Platform Kit do this by default. For a
private service mesh, clients may set `BRUNOST_JUDGE_CA_FILE` and the matching
`BRUNOST_JUDGE_CLIENT_CERT_FILE` / `BRUNOST_JUDGE_CLIENT_KEY_FILE` to enable
private-CA verification and mTLS without changing the Judge API contract.

Use `docker compose ... config --quiet` with all required variables supplied
before bringing up the hardened overlay. Never point a production service at
the reference Compose defaults or an untested host Docker socket.
