# Classic ICPC tasks

`kind: icpc` is Brunost Judge's deterministic batch-programming format. The
classic runner compiles one submitted source file, executes it once for each
private input, and checks its stdout. It is the right format for ordinary
algorithmic problems whose result is determined only by the input and
submission.

The complete runnable reference package is
[`examples/deterministic-sum`](../examples/deterministic-sum).

## Package boundary

```text
task/
├── judge.yaml
├── public/                    # statement, samples, visible assets
├── private/                   # reference source and other hidden assets
├── tests/
│   ├── small.in
│   ├── small.ans              # exactly one .ans or .out for an input
│   └── stress.in
│   └── stress.out
└── checker.py                 # optional trusted checker
```

Everything below `tests/` is judge-only data, even if a task author also
publishes matching samples under `public/`. The Docker evaluator receives the
task package in a root-only temporary filesystem; contestant processes receive
only stdin, their private build directory, and stdout/stderr. Do not use the
local process runner as evidence of this production boundary.

`private/` must exist for every task. A generated-answer reference program is
also required to live there, which prevents it from being accidentally copied
with participant-facing material.

## Manifest

Use a flat YAML manifest. This is a complete CPU task:

```yaml
version: 1
kind: icpc
runner: classic
language: cpp
runtime: python-3.13
time_limit_ms: 2000
memory_limit_mb: 512
output_limit_bytes: 1048576
network: disabled
scoring_mode: all_or_nothing
resource_class: cpu
required_capabilities: runtime:docker
answer_source: answer_key
```

| Field | Meaning |
| --- | --- |
| `language` | Submission language: `python`, `c`, `cpp`, or `rust`; common aliases such as `c++17` and `py` are accepted. |
| `runtime` | Evaluator runtime selected by the worker. If omitted it is `python-3.13`; production workers must map it to a certified, digest-pinned image. |
| `time_limit_ms` | Per-test wall-clock limit. Default: 2000 ms; valid range: 100–60000 ms. |
| `memory_limit_mb` | Per-process address-space limit. Default: 512 MiB; valid range: 64–4096 MiB. The container remains an outer limit. |
| `output_limit_bytes` | Maximum stdout per test. Default: 1048576 bytes; valid range: 1024–67108864 bytes. Stderr capture is separately capped at 1 MiB to prevent diagnostic floods. |
| `scoring_mode` | `all_or_nothing` gives 1 only for all accepted tests. `percentage` gives an equal share for every accepted test. |
| `resource_class` | Optional worker pool, for example `cpu` or `high-memory`. A registered task overrides the class supplied with an evaluation request. |
| `required_capabilities` | Optional comma-separated or inline-list worker labels. They are combined with operator-supplied requirements before claim. |
| `network` | Use `disabled`. The production Docker evaluator always has no network; `enabled` is rejected during task validation. |
| `answer_source` | `answer_key` (the default) reads committed answers. `reference` runs a hidden reference program to generate each answer. |

The package is authoritative at registration: its runtime, network policy,
resource class, and required capabilities are stored with the task rather than
being chosen by an evaluation caller. An evaluation's `timeout_seconds` can
shorten a task deadline, but cannot make it longer.

## Tests and answer keys

Put every judged input at `tests/**/*.in`. For `answer_source: answer_key`, put
one matching `tests/**/*.ans` **or** `tests/**/*.out` beside every input.
`brunost task validate` rejects missing answers, pairs that contain both
extensions, and answer files without a corresponding input. It also bounds a
package to 200 test inputs, 32 MiB per input, and 512 MiB total judged input and
answer data.

The default checker compares whitespace-separated output tokens. Thus trailing
whitespace and line wrapping are not significant, but a missing/extra token is
wrong. Each test gets an individual verdict (`OK`, `WA`, `TLE`, `OLE`, or
`RE`) plus measured time and output size in the result metrics. A compile
failure is reported as `CE` and has no test rows.

The worker derives a whole-evaluation deadline when the package is registered:

```text
5 s evaluator setup + 30 s per compile + (test count × per-test time)
```

Reference-answer tasks budget one compile and one test run for the reference,
as well as one for the submission. A package whose derived budget exceeds one
hour is rejected. This outer bound complements the per-test limit, so a large
test suite cannot silently use an unbounded worker slot.

## Hidden reference answers

Use a reference program when answer files would be too large or are generated
from an authoritative solution:

```yaml
answer_source: reference
reference_language: python
reference_entrypoint: private/reference.py
```

The runner compiles the reference in a separate temporary root and runs it on
each `.in` file before compiling the submission. Do not commit `.ans` or `.out`
files for a reference task—the validator rejects that ambiguous configuration.
The reference program is trusted task-author code, not participant code, so it
must be deterministic, bounded, and reviewed like evaluator code. Reference
and checker source files are limited to 1 MiB; Python references are parsed at
validation time so syntax errors do not wait until a live evaluation.

## Custom checkers

An optional root-level `checker.py` must define:

```python
def check(input_path: str, answer_path: str, output_path: str) -> bool | dict | float:
    ...
```

It can return a Boolean; a mapping with `ok`/`accepted`, optional `verdict`,
and optional `message`; or a finite number, where values at least `1.0` accept.
The checker runs inside the trusted evaluator boundary and can read private
task assets. It must never import participant files, contact the network, or
wait for external input. Validation checks that it is valid Python and defines
`check()` before registration; test it separately with adversarial outputs.

## Authoring and release checks

Run these before registering a new version:

```bash
brunost task validate ./tasks/my-problem
pytest -q ./tasks/my-problem/tests
```

Include tests for an accepted submission, a wrong answer, a timeout, malformed
output, and checker edge cases. Upload and register the validated directory as
an immutable artifact; changing any input, answer, checker, or manifest changes
the task digest. Re-register it under a new task reference/version instead of
mutating a contest task in place.

For an untrusted contest, also run the Docker integration test against the
same certified evaluator image described in
[`production.md`](production.md). A local run is useful for author feedback but
does not prove that task assets are hidden from contestant code.
