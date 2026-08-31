# Optimization tasks

Optimization tasks let a contestant program emit a candidate solution for
each input instance. A trusted author-owned evaluator decides whether the
candidate is feasible and computes its objective value. Contestant output is
never trusted to report its own score.

The complete minimal package is
[`../examples/optimization-basics`](../examples/optimization-basics).

The contestant is run once per instance. Its standard input is the instance
file and its standard output is the candidate solution consumed by the
evaluator. There is no required output format in the Judge itself: the task
author defines that format in `evaluate()`.

## Manifest

```yaml
version: 1
kind: optimization
runner: optimization
language: python
time_limit_ms: 2000
memory_limit_mb: 512
output_limit_bytes: 1048576
network: disabled
evaluation: evaluator:evaluate
objective_direction: maximize
score_mode: checker_score
aggregation: mean
# Alternatives: objective_direction: minimize; score_mode: baseline_ratio;
# aggregation: minimum or geometric_mean.
evaluator_entrypoint: private/evaluator.py
baseline_enabled: false
```

Every instance is stored as `tests/<name>.in`. Public examples may also be
copied to `public/instances/`; the Judge always evaluates the complete hidden
`tests/` set. The validator accepts at most 100 instances and limits each
instance, evaluator, and baseline to 1 MiB.

## Evaluator contract

`private/evaluator.py` must define:

```python
from pathlib import Path


def evaluate(input_path: str, output_path: str) -> dict:
    try:
        candidate = float(Path(output_path).read_text().strip())
    except (OSError, ValueError) as exc:
        from grader.optimization import InvalidOptimizationOutput

        raise InvalidOptimizationOutput("output must contain one finite number") from exc
    return {
        "feasible": candidate >= 0,
        "objective": candidate,
        "score": 1.0,  # required for checker_score, optional for baseline_ratio
    }
```

The return value must be an object with boolean `feasible`. Feasible results
must include a finite numeric `objective`. `checker_score` additionally
requires a finite score in `[0, 1]` for every feasible instance. An evaluator
may return `feasible: false` for a well-formed but infeasible solution, or
raise `InvalidOptimizationOutput` for malformed candidate output. Both cases
receive zero for that instance. Other evaluator exceptions, missing files,
and invalid evaluator return values are task errors and fail the evaluation.

`objective` is the author’s objective value, not automatically the score.
With `checker_score`, the evaluator owns the normalized score and must return
it in `[0, 1]`; `objective_direction` is retained in metadata for display and
does not alter that custom score. With `baseline_ratio`, the Judge derives the
score from the objective and the baseline, so the evaluator does not need to
return `score`.

The evaluator runs in a fresh short-lived process for every instance. Its
stdout and stderr are discarded, its memory is bounded by the task memory
limit, and its time budget is `max(1000 ms, 2 * time_limit_ms)`. This keeps a
broken or accidentally non-terminating evaluator from wedging the worker and
prevents evaluator state from leaking between instances. Contestant code is
subject to the manifest’s `time_limit_ms`, `memory_limit_mb`, and
`output_limit_bytes` on every instance. These limits apply independently to
each run, including the optional baseline. Evaluators and contestants receive
only a minimal execution environment; deployment credentials and unrelated
worker environment variables are not exposed to task code.

## Baseline normalization

Set `baseline_enabled: true` and provide `baseline_entrypoint` when using
`score_mode: baseline_ratio`. The baseline is run through the same input and
evaluator contract. For maximization, the instance score is
`candidate_objective / baseline_objective`; for minimization it is
`baseline_objective / candidate_objective`. Ratios are clamped to `[0, 1]`,
and objectives must be non-negative. This makes the baseline a normalization
reference, never a participant-visible score or test answer.

A zero baseline objective gives score `1` when the candidate is also optimal
or better, and otherwise `0`; a zero candidate objective is optimal for
minimization.

The final task score is the mean, minimum, or geometric mean of instance
scores, as selected by `aggregation`. The public task API exposes only the
statement, scoring policy, and public instance inputs; evaluator, baseline,
and hidden-instance contents stay private.

The result metadata has `schema_version: 1`, a stable per-instance `id`, the
candidate verdict, elapsed time, output byte count, feasibility, objective,
and derived score where applicable. Common verdicts are `OK`, `INFEASIBLE`,
`INVALID`, `TLE`, `OLE`, `RE`, and `CE`. A task-level `failed` result means
the package or author evaluator is invalid; it is distinct from a completed
submission with a zero score.
