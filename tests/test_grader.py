"""Grader-core tests: the IOAI metrics adapter + end-to-end scoring on synthetic tasks.

Runs with only numpy + pytest (no Brunost backend) — proving the grader is standalone.
"""

from __future__ import annotations

import textwrap

import numpy as np
import pytest

from grader.harness import normalize_result, run


def _write(path, content: str) -> None:
    with open(path, "w") as handle:
        handle.write(content)


# --- normalize_result: the IOAI -> canonical adapter -------------------------------

def test_normalize_plain_number():
    r = normalize_result(0.93)
    assert r["status"] == "completed"
    assert r["score"] == pytest.approx(0.93)
    assert r["metrics"]["public"] == pytest.approx(0.93)


def test_normalize_flat_split():
    r = normalize_result({"public": 0.98, "private": 0.97, "public_detail": {"Accuracy": 0.98}})
    assert r["score"] == pytest.approx(0.98)  # score == public (safe to show live)
    assert r["metrics"]["public"] == pytest.approx(0.98)
    assert r["metrics"]["private"] == pytest.approx(0.97)
    assert r["metrics"]["public_detail"] == {"Accuracy": 0.98}


def test_normalize_model_uses_private_as_official_score():
    r = normalize_result(
        {"public": 0.98, "private": 0.73, "metrics": {"metric": "accuracy"}},
        official_split="private",
        require_official=True,
    )
    assert r["score"] == pytest.approx(0.73)
    assert r["metrics"]["public"] == pytest.approx(0.98)
    assert r["metrics"]["private"] == pytest.approx(0.73)


def test_normalize_model_requires_private_score():
    r = normalize_result({"public": 0.98}, official_split="private", require_official=True)
    assert r["status"] == "failed"
    assert "private score" in r["failure_reason"]


def test_normalize_ioai_nested():
    r = normalize_result(
        {"score": {"public_a": 0.984, "private_b": 0.98, "public_detail": {"Accuracy": 0.984}}}
    )
    assert r["status"] == "completed"
    assert r["score"] == pytest.approx(0.984)
    assert r["metrics"]["public"] == pytest.approx(0.984)
    assert r["metrics"]["private"] == pytest.approx(0.98)


def test_normalize_rejects_garbage():
    assert normalize_result(True)["status"] == "failed"       # bool is not a score
    assert normalize_result("nope")["status"] == "failed"
    assert normalize_result({"metrics": {}})["status"] == "failed"   # no score at all
    assert normalize_result(float("inf"))["status"] == "failed"      # non-finite


def test_normalize_preserves_structured_test_metrics():
    """Structured test metrics survive scorer-result normalization."""
    test_metrics = [
        {
            "id": 1, "name": "Test group 1", "points": 30.0, "awarded": 30.0,
            "points_max": 30.0, "points_earned": 30.0, "verdict": "AC",
            "verdicts": ["s1a:AC"],
            "tests": [{"name": "s1a", "verdict": "AC", "time_ms": 12}],
        },
        {
            "id": 2, "name": "Test group 2", "points": 70.0, "awarded": 0.0,
            "points_max": 70.0, "points_earned": 0.0, "verdict": "TLE",
            "first_failed_test": "s2a",
            "verdicts": ["s2a:TLE"],
            "tests": [{"name": "s2a", "verdict": "TLE", "time_ms": 1999}],
        },
    ]
    r = normalize_result({"public": 30.0, "private": 30.0, "metrics": {"tests": test_metrics}})
    assert r["status"] == "completed"
    assert r["score"] == pytest.approx(30.0)
    assert r["metrics"]["tests"] == test_metrics


# --- run(): end-to-end on synthetic IOAI-shaped tasks ------------------------------

def test_run_csv_classification(tmp_path):
    """Antique-style: hidden label.csv vs an uploaded submission.csv, flat split."""
    assets, sub = tmp_path / "assets", tmp_path / "sub"
    assets.mkdir()
    sub.mkdir()
    _write(assets / "label.csv", "id,label\n1,a\n2,b\n3,a\n4,b\n")
    _write(sub / "submission.csv", "id,pred\n1,a\n2,b\n3,b\n4,b\n")  # 3/4 correct
    _write(assets / "metrics.py", textwrap.dedent('''
        import csv, os
        def _read(p):
            with open(p) as f:
                return {row["id"]: list(row.values())[1] for row in csv.DictReader(f)}
        def evaluate(submission_path, assets_path):
            labels = _read(os.path.join(assets_path, "label.csv"))
            preds = _read(os.path.join(submission_path, "submission.csv"))
            ids = sorted(labels)
            half = len(ids) // 2
            pub = sum(preds.get(i) == labels[i] for i in ids[:half]) / half
            prv = sum(preds.get(i) == labels[i] for i in ids[half:]) / (len(ids) - half)
            return {"public": pub, "private": prv, "public_detail": {"Accuracy": pub}}
    '''))
    r = run(str(sub), str(assets))
    assert r["status"] == "completed", r
    assert 0.0 <= r["score"] <= 1.0
    assert "public" in r["metrics"] and "private" in r["metrics"]


def test_run_npz_density(tmp_path):
    """Chicken_Counting-style: hidden labels.npz vs submission.npz, IOAI nested shape."""
    assets, sub = tmp_path / "assets", tmp_path / "sub"
    assets.mkdir()
    sub.mkdir()
    rng = np.random.default_rng(0)
    true_a = rng.random((5, 1, 8, 8))
    true_b = rng.random((5, 1, 8, 8))
    np.savez(assets / "labels.npz", true_a=true_a, true_b=true_b)
    np.savez(sub / "submission.npz", pred_a=true_a.copy(), pred_b=true_b.copy())  # perfect
    _write(assets / "metrics.py", textwrap.dedent('''
        import os, numpy as np
        def _counts(a): return a.reshape(a.shape[0], -1).sum(axis=1)
        def _score(pred, true):
            if (pred < 0).any(): return 0.0
            diff = np.abs(_counts(pred) - _counts(true))
            denom = np.maximum(_counts(true), 1.0)
            return float(np.mean(1.0 - np.minimum(diff / denom, 1.0)))
        def evaluate(submission_path, assets_path):
            L = np.load(os.path.join(assets_path, "labels.npz"))
            S = np.load(os.path.join(submission_path, "submission.npz"))
            return {"score": {"public_a": _score(S["pred_a"], L["true_a"]),
                              "private_b": _score(S["pred_b"], L["true_b"])}}
    '''))
    r = run(str(sub), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(1.0)
    assert r["metrics"]["private"] == pytest.approx(1.0)


def test_run_missing_scorer(tmp_path):
    assets, sub = tmp_path / "assets", tmp_path / "sub"
    assets.mkdir()
    sub.mkdir()
    r = run(str(sub), str(assets))
    assert r["status"] == "failed"
    assert "metrics.py" in r["failure_reason"]


def test_run_scorer_raises(tmp_path):
    assets, sub = tmp_path / "assets", tmp_path / "sub"
    assets.mkdir()
    sub.mkdir()
    _write(assets / "metrics.py", "def evaluate(s, a):\n    raise ValueError('boom')\n")
    r = run(str(sub), str(assets))
    assert r["status"] == "failed"
    assert "boom" in r["failure_reason"]


def test_run_bad_submission_is_contained(tmp_path):
    """A corrupt/missing submission file must fail gracefully, not crash the sandbox."""
    assets, sub = tmp_path / "assets", tmp_path / "sub"
    assets.mkdir()
    sub.mkdir()
    _write(assets / "metrics.py", textwrap.dedent('''
        import os, numpy as np
        def evaluate(submission_path, assets_path):
            np.load(os.path.join(submission_path, "submission.npz"))  # missing -> raises
            return 1.0
    '''))
    r = run(str(sub), str(assets))
    assert r["status"] == "failed"
    assert r["score"] == 0.0


def _write_train_predict_task(
    root,
    submission_code: str,
    *,
    training_ms: int = 5_000,
    total_ms: int | None = None,
    baseline: bool = False,
    evaluator_code: str | None = None,
    public: bool = False,
    post: bool = False,
) -> None:
    (root / "public" / "datasets").mkdir(parents=True)
    (root / "private" / "datasets").mkdir(parents=True)
    (root / "private" / "post").mkdir(parents=True)
    (root / "public" / "datasets" / "train.csv").write_text("x,label\n1,0\n", encoding="utf-8")
    (root / "private" / "datasets" / "test.csv").write_text("x\n2\n", encoding="utf-8")
    (root / "private" / "datasets" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
    if public:
        (root / "public" / "datasets" / "public-test.csv").write_text("x\n3\n", encoding="utf-8")
        (root / "public" / "datasets" / "public-labels.csv").write_text("label\n1\n", encoding="utf-8")
    if post:
        (root / "private" / "post" / "training.csv").write_text("x,label\n4,0\n", encoding="utf-8")
        (root / "private" / "post" / "test.csv").write_text("x\n5\n", encoding="utf-8")
        (root / "private" / "post" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
        (root / "post_evaluator.py").write_text("def evaluate(predictions_path, labels_path): return 0.55\n", encoding="utf-8")
    total = total_ms if total_ms is not None else training_ms + 25_000
    lines = [
        "version: 2",
        "kind: model",
        "runner: model",
        "model_contract: train_predict_v2",
        "runtime: python-3.13-ml-v1",
        "evaluation: evaluator:evaluate",
        "network: disabled",
        f"time_limit_ms: {total}",
        f"training_time_limit_ms: {training_ms}",
        "prediction_time_limit_ms: 10000",
        "evaluator_time_limit_ms: 10000",
        "memory_limit_mb: 512",
        "model_max_bytes: 64000000",
        "training_dataset: public/datasets/train.csv",
        "private_test_dataset: private/datasets/test.csv",
        "private_labels_dataset: private/datasets/labels.csv",
        "submission_entrypoint: submission.py",
        "baseline_enabled: " + ("true" if baseline else "false"),
        "post_competition_enabled: " + ("true" if post else "false"),
    ]
    if public:
        lines.extend(["public_test_dataset: public/datasets/public-test.csv", "public_labels_dataset: public/datasets/public-labels.csv"])
    if baseline:
        lines.append("baseline_entrypoint: private/baseline.py")
    if post:
        lines.extend([
            "post_training_dataset: private/post/training.csv",
            "post_test_dataset: private/post/test.csv",
            "post_labels_dataset: private/post/labels.csv",
            "post_training_time_limit_ms: 600000",
            "post_prediction_time_limit_ms: 10000",
            "post_evaluator_time_limit_ms: 60000",
            "post_evaluator_entrypoint: post_evaluator.py",
        ])
    (root / "judge.yaml").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    (root / "evaluator.py").write_text(
        evaluator_code or """import os\nfrom pathlib import Path\n\ndef evaluate(predictions_path, labels_path):\n    assert Path(predictions_path).is_file()\n    assert Path(labels_path).read_text() == 'label\\n1\\n'\n    split = os.environ.get('BRUNOST_ML_SPLIT')\n    return 0.25 if split == 'public' else 0.75\n""",
        encoding="utf-8",
    )
    if baseline:
        (root / "private" / "baseline.py").write_text(
            "def train(train_dataset, model_path):\n    open(model_path, 'wb').write(b'baseline')\n\ndef predict(model_path, test_dataset, predictions_path):\n    open(predictions_path, 'w').write('baseline\\n')\n",
            encoding="utf-8",
        )
    submission = root.parent / "submission"
    submission.mkdir()
    (submission / "submission.py").write_text(submission_code, encoding="utf-8")


def test_run_model_training_submission_uses_private_score_and_allows_many_epochs(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        """from pathlib import Path\ndef train(train_dataset, model_path):\n    for _ in range(2000):\n        pass\n    Path(model_path).write_bytes(b'model')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text('prediction\\n')\n""",
        public=True,
    )
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(0.75)
    assert r["metrics"]["public"] == pytest.approx(0.25)
    assert r["metrics"]["private"] == pytest.approx(0.75)
    assert r["metrics"]["profile"] == "live"


def test_run_model_training_submission_times_out(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(assets, """import time\ndef train(train_dataset, model_path):\n    time.sleep(1)\ndef predict(model_path, test_dataset, predictions_path):\n    pass\n""", training_ms=100)
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "failed"
    assert r["metrics"]["verdict"] == "time_limit_exceeded"


def test_run_model_training_submission_can_use_optional_baseline(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "from pathlib import Path\ndef train(train_dataset, model_path):\n    Path(model_path).write_bytes(b'baseline-test')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text('prediction\\n')\n",
        baseline=True,
    )
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(0.75)


def test_run_model_training_uses_one_total_budget_for_baseline_and_submission(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "import time\nfrom pathlib import Path\ndef train(train_dataset, model_path):\n    time.sleep(0.15)\n    Path(model_path).write_bytes(b'model')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text('prediction\\n')\n",
        training_ms=220,
        total_ms=250,
        baseline=True,
    )
    (assets / "private" / "baseline.py").write_text(
        "import time\nfrom pathlib import Path\ndef train(train_dataset, model_path):\n    time.sleep(0.15)\n    Path(model_path).write_bytes(b'baseline')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text('baseline\\n')\n",
        encoding="utf-8",
    )
    r = run(str(assets.parent / "submission"), str(assets))
    assert r["status"] == "failed", r
    assert r["metrics"]["verdict"] == "time_limit_exceeded"


def test_run_model_training_does_not_inherit_unapproved_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BRUNOST_PRIVATE_SENTINEL", "must-not-leak")
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "from pathlib import Path\ndef train(train_dataset, model_path):\n    Path(model_path).write_text('clean' if not __import__('os').environ.get('BRUNOST_PRIVATE_SENTINEL') else 'leaked')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text(Path(model_path).read_text())\n",
        evaluator_code="""from pathlib import Path

def evaluate(predictions_path, labels_path):
    return 1.0 if Path(predictions_path).read_text() == 'clean' else 0.0
""",
    )
    r = run(str(assets.parent / "submission"), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(1.0)


def test_run_model_training_rejects_empty_prediction_output(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "from pathlib import Path\ndef train(train_dataset, model_path):\n    Path(model_path).write_bytes(b'model')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text('')\n",
    )
    r = run(str(assets.parent / "submission"), str(assets))
    assert r["status"] == "failed"
    assert "empty" in r["failure_reason"]


def test_run_model_training_rejects_empty_model(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "from pathlib import Path\ndef train(train_dataset, model_path):\n    Path(model_path).write_bytes(b'')\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_text('prediction\\n')\n",
    )
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "failed"
    assert "model.bin" in r["failure_reason"]


def test_run_model_post_competition_profile_uses_new_data(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "from pathlib import Path\ndef train(train_dataset, model_path):\n    Path(model_path).write_bytes(Path(train_dataset).read_bytes())\ndef predict(model_path, test_dataset, predictions_path):\n    Path(predictions_path).write_bytes(Path(model_path).read_bytes())\n",
        post=True,
    )
    monkeypatch.setenv("BRUNOST_EVALUATION_PROFILE", "post_competition")
    r = run(str(assets.parent / "submission"), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(0.55)
    assert r["metrics"]["profile"] == "post_competition"


def test_grader_has_no_backend_imports():
    """Extraction rule (ADR-0010): the grader core must not import the Brunost backend."""
    import pathlib
    import re

    grader_dir = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for py in grader_dir.rglob("*.py"):
        if "tests" in py.parts:
            continue
        if re.search(r"^\s*(from|import)\s+app(\.|\s|$)", py.read_text(), re.MULTILINE):
            offenders.append(str(py.relative_to(grader_dir)))
    assert not offenders, f"grader core must stay backend-independent; found app imports in: {offenders}"
