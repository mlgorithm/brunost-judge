# Optimization tasks

Optimization tasks let a contestant program emit a candidate solution for
each input instance. A trusted author-owned evaluator decides whether the
candidate is feasible and computes its objective value. Contestant output is
never trusted to report its own score.

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
objective_direction: maximize # or minimize
score_mode: checker_score # or baseline_ratio
aggregation: mean # minimum or geometric_mean
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
    candidate = float(Path(output_path).read_text().strip())
    return {
        "feasible": candidate >= 0,
        "objective": candidate,
        "score": 1.0,  # required for checker_score, optional for baseline_ratio
    }
```

The return value must be an object with boolean `feasible`. Feasible results
must include a finite numeric `objective`. `checker_score` additionally
requires a finite score in `[0, 1]` for every feasible instance. Infeasible,
runtime-error, time-limit, and output-limit instances receive zero for that
instance; task errors such as a broken evaluator fail the evaluation.

## Baseline normalization

Set `baseline_enabled: true` and provide `baseline_entrypoint` when using
`score_mode: baseline_ratio`. The baseline is run through the same input and
evaluator contract. For maximization, the instance score is
`candidate_objective / baseline_objective`; for minimization it is
`baseline_objective / candidate_objective`. Ratios are clamped to `[0, 1]`,
and objectives must be non-negative. This makes the baseline a normalization
reference, never a participant-visible score or test answer.

The final task score is the mean, minimum, or geometric mean of instance
scores, as selected by `aggregation`. The public task API exposes only the
statement, scoring policy, and public instance inputs; evaluator, baseline,
and hidden-instance contents stay private.
