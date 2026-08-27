# Model and ML tasks

`model` tasks support two modes:

- `scorer`: the legacy contract. The task scorer directly reads the uploaded
  artifact and computes the result.
- `python_code`: the standard train/predict contract. The Judge runs the
  participant's Python entrypoint, then invokes the private scorer.

The second mode is recommended for new Premium ML tasks.

Premium publishes `python-3.13-ml-v1` for Python training tasks. The runtime
image contains the portable CPU stack (`numpy`, `pandas`, `scikit-learn`, and
`pyarrow`) in addition to the normal Judge tools. Operators must map that
runtime to a separately built, digest-pinned sandbox image; the Judge never
silently runs an ML task in the smaller default image.

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
prediction_max_bytes: 10000000
official_split: private
baseline_enabled: false
metric: accuracy
direction: maximize
aggregation: mean
```

`time_limit_ms` is the complete evaluator budget shared by the optional baseline,
participant, and scorer. `training_time_limit_ms` is the per-process ceiling for
the baseline and participant, but neither phase can extend the total deadline.
Epoch counts are not inspected or capped: 2,000 epochs are valid if they finish
before the remaining budget. A timeout, crash, memory violation, missing output,
empty output, or output larger than `prediction_max_bytes` receives no score.
The default prediction limit is 10 MB; task manifests may raise it up to 64 MB.

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

The participant and scorer run in separate processes. Participant code receives
only the documented ML variables plus basic runtime variables; private labels,
baseline predictions, and scorer-only variables are not present during training.
The scorer has a bounded JSON response and its own time and memory limits.

## Baselines

Set `baseline_enabled: true` and include `private/baseline.py` to enable a
baseline. It receives the same public dataset, private test features, seed, and
resource limits. Its predictions are exposed to the scorer through
`BRUNOST_ML_BASELINE_PREDICTIONS_PATH`.

The baseline is not used to normalize participant scores automatically. It is a
reference and task-health check; the official score remains the participant's
private metric.

## Runtime image configuration

Keep the default image for ordinary Python tasks and map the ML runtime explicitly
in production:

```text
BRUNOST_JUDGE_SANDBOX_IMAGE=...@sha256:<digest>
BRUNOST_JUDGE_SANDBOX_IMAGES={"python-3.13":"...@sha256:<digest>","python-3.13-ml-v1":"...@sha256:<digest>"}
```

`BRUNOST_JUDGE_SANDBOX_IMAGES` is JSON. Every image reference must be pinned by
digest in production, and a task with an unmapped non-default runtime fails
closed before Docker starts.
