# Changelog

This project follows [Semantic Versioning](https://semver.org/). Public API,
task-schema, result, and deployment changes are described here and in the tagged
GitHub release. See [`docs/compatibility.md`](docs/compatibility.md) for the
support rules.

## 1.3.1 — 2026-08-31

### Added

- Artifact-first task/submission registration, durable signed callbacks,
  scoped service and worker credentials, capability scheduling, and deployment
  drills.
- Built-in task runners for coding, model, quiz, optimization, interactive,
  generic scorer, agent, and game contracts.
- Versioned `train_predict_v2` model packages and immutable result artifacts.

### Changed

- `coding` is the preferred deterministic programming task kind. `icpc`
  remains a legacy alias for existing packages.
- Browser-local runtimes are refused as Judge task runtimes.

## Earlier releases

Historic pre-1.0 tags are retained in Git history. They predate the current
public contract and should not be treated as compatible stable releases.
