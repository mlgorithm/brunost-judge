# Security policy

Please do not report a sandbox escape, credential disclosure, or other
security-sensitive issue in a public issue. Contact the maintainers privately
with a clear description, affected version, reproduction steps, and impact.

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
