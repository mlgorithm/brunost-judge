# Release process

This checklist turns a passing branch into a reproducible public Judge release.
It is separate from the operator canary in [`rollout.md`](rollout.md): a code
release proves the public contract, while an operator canary proves a particular
deployment.

## Prepare

1. Update `src/brunost_judge/version.py`. Package metadata, API version, and
   worker user agent derive from this one value.
2. Add concise entries to [`../CHANGELOG.md`](../CHANGELOG.md), including every
   deprecation, schema change, migration, and security fix.
3. Review [`compatibility.md`](compatibility.md), the OpenAPI diff, and task
   schemas. Public breaking changes require a new API or manifest version.
4. Confirm README quick-start commands and every fenced task-manifest snippet
   against the current validator.

## Verify

Run the normal test suite and public-contract check:

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest -q
python scripts/check_public_contract.py
python scripts/export_openapi.py /tmp/brunost-judge-openapi.json
```

For a release that changes a sandbox or runner, execute the Docker integration
suite against the digest-pinned image and retain its output. Perform the
artifact/worker/callback canary described in [`rollout.md`](rollout.md) before
an official contest deployment.

## Publish

1. Build the wheel and source distribution from the verified commit.
2. Tag that exact commit as `vX.Y.Z`; do not move a published tag.
3. Publish release notes, artifact checksums, container image digests, SBOM or
   provenance information where available, and the OpenAPI/schema snapshots.
4. Record the tagged commit, reviewer approvals, CI run, and canary evidence.
5. Keep a rollback path to the previous image and preserve compatibility for
   integrations during the announced support window.

Never call an untagged branch or a Compose development default an official
release.
