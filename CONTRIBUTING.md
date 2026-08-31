# Contributing to Brunost Judge

Brunost Judge is developed in the open. Small pull requests are welcome,
especially task-package examples, contract tests, documentation, and provider
adapters. Read [the governance policy](GOVERNANCE.md),
[compatibility policy](docs/compatibility.md), and [security policy](SECURITY.md)
before changing a public or security-sensitive surface.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests
python scripts/check_public_contract.py
```

The `grader` package is kept as a compatibility import while the public API is
introduced as `brunost_judge`. The core package must remain independent of any
LMS, contest, or product backend.

## Choosing a change

- Document and test any public API, task-manifest, result, callback, or plugin
  change. Update the relevant schema and compatibility note with the code.
- Keep new user-facing task packages within the `coding`, `model`, `quiz`, and
  `optimization` taxonomy unless you are extending an explicitly advanced
  runner contract.
- Use the committed examples as public fixtures. Do not commit credentials,
  private contest data, hidden labels, or participant submissions.
- Changes to a sandbox, artifact integrity, authentication, worker lease, or
  callback boundary need a threat-model note and targeted regression tests.

## Pull requests

- Keep changes focused and include tests for contract changes.
- Do not add credentials, private contest data, or hidden labels.
- Do not weaken sandbox, immutability, or callback guarantees for convenience.
- Document any public API or task-format change.
- Run the same focused checks that CI runs. If Docker integration is relevant,
  run it against a digest-pinned evaluator image and state the result in the PR.

## Review and release

Maintainers review public-contract changes for backward compatibility and
release notes. Breaking changes require a new API or manifest version and an
explicit migration path; do not silently repurpose existing fields. Release
steps are documented in [`docs/releases.md`](docs/releases.md).
