# Model and ML tasks

`model` tasks use one contract everywhere: participant code trains an artifact,
the Judge runs prediction against each evaluation split, and an author-owned
evaluator scores the resulting predictions. There is no artifact-only model
mode and no model-specific `metrics.py` contract.

The complete minimal package is [`../examples/model-basics`](../examples/model-basics).

## Package layout

```text
judge.yaml
evaluator.py
public/statement.md
public/datasets/training.csv
public/datasets/public-test.csv       optional
public/datasets/public-labels.csv     optional
private/datasets/test.csv
private/datasets/labels.csv
private/baseline.py                   optional
private/post/training/...             optional
private/post/test/...
private/post/labels/...
post_evaluator.py                      optional
```

The training dataset and an optional public test split are visible to
participants. Private test features and labels are copied into isolated Judge
workspaces only when the corresponding phase needs them. Private labels are
never available to participant code.

## Manifest

```yaml
version: 2
kind: model
runner: model
model_contract: train_predict_v2
runtime: python-3.13-ml-v1
evaluation: evaluator:evaluate
network: disabled
time_limit_ms: 150000
training_time_limit_ms: 120000
prediction_time_limit_ms: 10000
evaluator_time_limit_ms: 10000
memory_limit_mb: 2048
model_max_bytes: 64000000
training_dataset: public/datasets/training.csv
public_test_dataset: public/datasets/public-test.csv
public_labels_dataset: public/datasets/public-labels.csv
private_test_dataset: private/datasets/test.csv
private_labels_dataset: private/datasets/labels.csv
submission_entrypoint: submission.py
baseline_enabled: false
post_competition_enabled: false
```

The two public fields must be declared together. `model_max_bytes` limits the
artifact produced by `train()` and is also bounded by the Judge's 64 MB safety
limit. The total time limit is the complete budget; each phase also has its own
ceiling. Task publishers calculate the total from the phase limits when they
publish a package.

## Submission contract

The submitted module must define these functions:

```python
def train(train_dataset: str, model_path: str) -> None:
    # Read the visible training dataset and write a non-empty model artifact.
    ...


def predict(model_path: str, test_dataset: str, predictions_path: str) -> None:
    # Read the model and test features and write a non-empty prediction file.
    ...
```

Training and prediction run in separate processes and separate workspaces.
Training cannot see the test features, private labels, or predictions. The
artifact is copied read-only into prediction workspaces. Each prediction file
must be non-empty and stay within the Judge output limit.

Epoch counts are intentionally not interpreted by the Judge. A submission may
run 2,000 epochs or any other number as long as it finishes within the training
time limit and memory limit. The Judge terminates the process when the limit is
reached.

## Evaluator contract

`evaluator.py` defines the author-owned scoring function:

```python
def evaluate(predictions_path: str, labels_path: str) -> float | dict:
    # Read only these two files and return the score for this split.
    return 0.0
```

It may return a finite number or:

```python
{
    "score": 0.91,
    "metrics": {"accuracy": 0.91},
}
```

The Judge evaluates the public split first when it exists, then the private
split. The participant's private score is the official live score. Baseline
scores are reported as diagnostic metrics and never replace the participant's
score. Returning the old `public`/`private` mapping is rejected for model v2.

## Baselines

With `baseline_enabled: true`, `private/baseline.py` must implement the same
`train()` and `predict()` functions as a participant submission. It runs in an
independent workspace and uses the same datasets and limits. It is useful for
checking that the task data and evaluator are healthy; it is not an automatic
normalization factor.

## Post-competition evaluation

Authors can enable a second, hidden leaderboard profile:

```yaml
post_competition_enabled: true
post_training_dataset: private/post/training/training.csv
post_test_dataset: private/post/test/test.csv
post_labels_dataset: private/post/labels/labels.csv
post_training_time_limit_ms: 600000
post_prediction_time_limit_ms: 10000
post_evaluator_time_limit_ms: 60000
post_evaluator_entrypoint: post_evaluator.py
```

The same submitted module is trained from scratch on the new training data,
evaluated on the new hidden test set, and scored with `post_evaluator.py` (or
the live evaluator if no separate code is supplied). Workers select this
profile through execution metadata; it is not participant-controlled. The
integrating platform can keep the result in a separate post-competition
leaderboard.

## Runtime image

`python-3.13-ml-v1` is the reference ML runtime. Operators must map it to a
digest-pinned sandbox image containing the supported CPU ML libraries:

```text
BRUNOST_JUDGE_SANDBOX_IMAGES={"python-3.13":"...@sha256:<digest>","python-3.13-ml-v1":"...@sha256:<digest>"}
```

If the ML runtime is not mapped, the Judge fails closed before starting the
container. The standard Python image is not used as an implicit fallback.
