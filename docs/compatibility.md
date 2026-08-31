# Compatibility policy

Brunost Judge exposes two separately versioned public contracts: the HTTP API
and task packages. The release version is defined once in
[`src/brunost_judge/version.py`](../src/brunost_judge/version.py); distribution
metadata, the API, and worker user agent derive from it.

## HTTP API

- `/v1` is the stable API namespace. Additive response fields and endpoints may
  be introduced in a minor release.
- Existing request fields and endpoints remain supported for at least one major
  release cycle. Deprecated fields are marked in OpenAPI where possible.
- A breaking HTTP change requires a new namespace (for example `/v2`) and a
  documented migration path.
- Every submission mutation requires an idempotency key. Retry the same request
  with the same key; do not change task, artifact, callback, or queue fields
  under a reused key.
- Callbacks are at-least-once. Consumers must verify the signed raw payload and
  store `X-Brunost-Judge-Event-ID` durably before applying the result.

## Task packages

- `judge.yaml` v1 is used by coding, quiz, optimization, generic scorer,
  interactive, agent, and game packages. Model packages use v2 only.
- The schemas under [`../schemas`](../schemas) are versioned alongside the
  release. They are intentionally permissive about fields whose source/file
  constraints require the package validator.
- A compatible package change adds an optional field with a safe default. A
  change to kind, runner, evaluator entrypoint, runtime, scoring semantics, or
  hidden data layout requires a new task artifact and task reference.
- `icpc` is a permanent legacy alias of `coding` for the v1 classic runner;
  new integrations must use `coding`. `ioai` and `output-only` remain
  supported generic scorer types, but neither is required by another family.
- Browser-only runtimes are deliberately outside this contract. The Judge
  refuses them at validation and registration.

## Plugin and result contracts

- The agent wire protocol and runner-plugin protocol are separately versioned.
  Unknown additive JSON fields are tolerated where the protocol specifies.
- Results expose `result_version: 1`. Additive metric fields are permitted;
  consumers must not assume undeclared metric keys.
- Third-party workers and evaluator plugins should run the conformance helpers
  from `brunost_judge.conformance` against every supported Judge release.

The release checklist in [`releases.md`](releases.md) requires an OpenAPI diff,
schema review, and compatibility note for every public contract change.
