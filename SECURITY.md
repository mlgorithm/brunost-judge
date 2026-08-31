# Security policy

## Reporting a vulnerability

Do not report a sandbox escape, credential disclosure, private-task-data leak,
or other security-sensitive issue in a public issue. Use the repository's
[private GitHub security-advisory form](https://github.com/mlgorithm/brunost-judge/security/advisories/new).
Include the affected release/commit, a minimal reproduction, impact, and any
suggested mitigation. Do not attach live credentials, contestant submissions,
or hidden task data.

Maintainers will acknowledge a report, triage its severity and affected
versions, and coordinate a fix before public disclosure. Please give
maintainers reasonable time to investigate and release a fix before discussing
details publicly. If private reporting is unavailable, contact a repository
maintainer through GitHub without publishing exploit details.

## Supported versions

Security fixes target the latest stable release on `main`. Operators running an
older release should upgrade after reviewing its release notes and deployment
migration guidance. A release advisory identifies affected versions, mitigation,
and the first fixed version.

The judge executes untrusted contestant and task code. Treat deployment
configuration, runtime images, worker hosts, object storage, and hidden task
assets as part of the security boundary.

For a deployed API, set `BRUNOST_JUDGE_REQUIRE_API_TOKEN=true` and use a random
`BRUNOST_JUDGE_API_TOKEN`. Restrict callback destinations with
`BRUNOST_JUDGE_CALLBACK_HOSTS`. Workers can additionally sign every callback
with `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET`; receivers should verify the
timestamp/signature pair using `brunost_judge.sdk.JudgeClient.verify_callback`
and reject timestamps older than five minutes.

Set `BRUNOST_JUDGE_REQUIRE_IDEMPOTENCY_HEADER=true` at the HTTP edge so the
`Idempotency-Key` header must match the request body for evaluation submissions.
The SDK and Platform Kit send that header automatically. Authenticated HTTP
clients reject redirects and cap response bodies; private deployments may add
CA and mTLS files with the `BRUNOST_JUDGE_*_FILE` transport settings.

For distributed workers, also set `BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true`.
Create short-lived join tokens through the operator API or console, enroll each
node once, and keep the returned worker credential in a mode-`0600` node
configuration. Never copy the global API token to a worker. Re-enrolling a node
rotates its worker credential; revoke the old credential before investigating a
lost node.

The reference Docker worker is not a complete sandbox for adversarial code.
Official contests must run the worker inside a separately hardened runtime
(gVisor, Kata, Firecracker, or equivalent), disable network egress, apply CPU,
memory, process, and disk quotas, and pin task/runtime images by digest.

## Scope

The security boundary includes the Judge API, worker credentials, callbacks,
artifact store, evaluator image, worker host, and private task assets. It does
not include browser-local runtimes or an integrating platform's identity,
leaderboard, and notification systems. See [production controls](docs/production.md)
for required deployment hardening.
