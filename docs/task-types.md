# Task types and manifest contracts

This is the public type matrix for Brunost Judge. A task package is a directory
with a flat `judge.yaml`; package files and source-code boundaries are validated
by `brunost task validate`. The JSON Schemas in [`../schemas`](../schemas) are
the machine-readable representation for tooling. They describe the manifest
shape, while the validator remains authoritative for required package files,
private-path boundaries, limits, and evaluator source.

## Current task families

| Family | `kind` | Manifest | Runner | Evaluation kind | Guide |
| --- | --- | --- | --- | --- | --- |
| Deterministic programming | `coding` | v1 | `classic` | `batch` | [coding tasks](classic-tasks.md) |
| Machine learning | `model` | v2 | `model` | `batch` | [model tasks](model-tasks.md) |
| Knowledge assessment | `quiz` | v1 | `quiz` | `batch` | [quiz tasks](quiz-tasks.md) |
| Optimization | `optimization` | v1 | `optimization` | `batch` | [optimization tasks](optimization-tasks.md) |

New user-facing task packages should use those four kinds. They are neutral
task-family names: an integration does not need an ICPC, IOI, or platform label
to register or execute them.

## Advanced and compatibility kinds

| `kind` | Status | Purpose | Evaluation kind | Contract |
| --- | --- | --- | --- | --- |
| `icpc` | Legacy alias | Existing deterministic coding packages | `batch` | Same as `coding`; do not use for new packages. |
| `ioai`, `output-only` | Maintained advanced | Trusted generic scorer and private assets | `batch` | `scorer.metrics:evaluate` or legacy `metrics:evaluate`. |
| `interactive` | Maintained advanced | Line-oriented interactor problems | `interactive` | Classic limits plus `interactor.py`. |
| `agent` | Maintained advanced | Single-agent runner-plugin execution | `agent` | Versioned plugin protocol. |
| `game` | Maintained advanced | Multi-agent/referee matches | `match` | Versioned plugin protocol and registered agents. |

`ioai` is a historical scorer type, not a prerequisite for any current task
family. `icpc` is an alias retained only for package compatibility. Browser
runtimes such as Pyodide, WASM, and CheerpX are not Judge task kinds or
runtimes; they belong to a browser-local environment and official results are
re-executed by the registered Judge runtime.

## Type invariants

- `kind`, manifest version, runner, runtime, and evaluator are package-owned.
  Registration records them with an immutable task artifact; an evaluation
  request cannot replace them.
- A registered task reference always identifies one immutable task artifact.
  Publish changed task content under a new version/reference.
- `coding` scores deterministically using all-or-nothing or equal percentage
  scoring. `quiz` evaluates an answer file without participant code.
- `model` uses only `train_predict_v2`: a submission trains, produces
  predictions for public/private splits, and an author evaluator scores them.
  Optional baselines and post-competition profiles remain task-owned.
- `optimization` runs participant code per instance and delegates feasibility
  and objective decisions to a trusted author evaluator.
- An evaluation's `evaluation_kind` must match its registered task type.

Use [`compatibility.md`](compatibility.md) before changing a manifest, API, or
runner contract.
