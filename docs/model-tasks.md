# Model and ML tasks

`model` tasks support two modes:

- `scorer`: the legacy contract. The task scorer directly reads the uploaded
  artifact and computes the result.
- `python_code`: the standard train/predict contract. The Judge runs the
  participant's Python entrypoint, then invokes the private scorer.

The second mode is recommended for new Premium ML tasks.

## Package layout

```text
judge.yaml
public/datasets/train.csv
private/datasets/test.csv
private/datasets/test_labels.csv
private/baseline.py             optional
scorer/metrics.py
```

`test.csv` contains features only. `test_labels.csv` is private ground truth and
is available to `scorer/metrics.py`, never to the participant process.

## Manifest

```yaml
version: 1
kind: model
runner: model
runtime: python-3.13-ml-v1
scoring: scorer.metrics:evaluate
network: disabled
time_limit_ms: 125000
training_time_limit_ms: 120000
memory_limit_mb: 2048
public_dataset: public/datasets/train.csv
hidden_dataset: private/datasets/test.csv
hidden_labels_dataset: private/datasets/test_labels.csv
submission_mode: python_code
submission_language: python
submission_entrypoint: submission.py
prediction_output: predictions.csv
official_split: private
baseline_enabled: false
metric: accuracy
direction: maximize
aggregation: mean
```

`time_limit_ms` is the complete evaluator budget. `training_time_limit_ms` is
the hard limit for the participant (and optional baseline) process. Epoch counts
are not inspected or capped: 2,000 epochs are valid if they finish before the
limit. A timeout, crash, memory violation, missing output, or invalid prediction
file receives no score.

## Submission contract

The participant entrypoint is executed once with:

```text
BRUNOST_ML_PUBLIC_DATASET=/.../input/public/train.csv
BRUNOST_ML_PRIVATE_DATASET=/.../input/private/test.csv
BRUNOST_ML_OUTPUT_PATH=/.../output/predictions.csv
BRUNOST_ML_SEED=42
```

The private dataset path contains features only. The output format is task-defined
but must be a file at the configured path. The scorer can also read:

```text
BRUNOST_ML_PREDICTIONS_PATH
BRUNOST_ML_PRIVATE_LABELS
BRUNOST_ML_BASELINE_PREDICTIONS_PATH   optional
```

The scorer must return either a private numeric score or both public and private
scores:

```python
def evaluate(submission_path: str, assets_path: str) -> dict:
    private_score = ...
    public_score = ...
    return {
        "public": public_score,
        "private": private_score,
        "metrics": {"metric": "accuracy"},
    }
```

For this mode the Judge always uses `private` as the canonical `score`. Returning
only a public value is an invalid result. The Premium platform is responsible for
deciding when public/private details are visible to participants.

## Baselines

Set `baseline_enabled: true` and include `private/baseline.py` to enable a
baseline. It receives the same public dataset, private test features, seed, and
resource limits. Its predictions are exposed to the scorer through
`BRUNOST_ML_BASELINE_PREDICTIONS_PATH`.

The baseline is not used to normalize participant scores automatically. It is a
reference and task-health check; the official score remains the participant's
private metric.
