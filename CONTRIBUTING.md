# Contributing to Brunost Judge

Brunost Judge is developed in the open. Small pull requests are welcome,
especially task-package examples, contract tests, documentation, and provider
adapters.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

The `grader` package is kept as a compatibility import while the public API is
introduced as `brunost_judge`. The core package must remain independent of any
NOKI/Brunost platform backend.

## Pull requests

- Keep changes focused and include tests for contract changes.
- Do not add credentials, private contest data, or hidden labels.
- Do not weaken sandbox, immutability, or callback guarantees for convenience.
- Document any public API or task-format change.
