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


def _write_train_predict_task(root, submission_code: str, *, training_ms: int = 5_000, baseline: bool = False) -> None:
    (root / "public" / "datasets").mkdir(parents=True)
    (root / "private" / "datasets").mkdir(parents=True)
    (root / "scorer").mkdir()
    (root / "public" / "datasets" / "train.csv").write_text("x,label\n1,0\n", encoding="utf-8")
    (root / "private" / "datasets" / "test.csv").write_text("x\n2\n", encoding="utf-8")
    (root / "private" / "datasets" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
    (root / "judge.yaml").write_text(
        "\n".join([
            "version: 1",
            "kind: model",
            "runner: model",
            "runtime: python-3.13-ml-v1",
            "scoring: scorer.metrics:evaluate",
            "network: disabled",
            f"time_limit_ms: {training_ms + 5000}",
            "training_time_limit_ms: " + str(training_ms),
            "memory_limit_mb: 512",
            "public_dataset: public/datasets/train.csv",
            "hidden_dataset: private/datasets/test.csv",
            "hidden_labels_dataset: private/datasets/labels.csv",
            "submission_mode: python_code",
            "submission_language: python",
            "submission_entrypoint: submission.py",
            "prediction_output: predictions.csv",
            "official_split: private",
            "baseline_enabled: " + ("true" if baseline else "false"),
        ]) + "\n",
        encoding="utf-8",
    )
    (root / "scorer" / "metrics.py").write_text(
        """import os\n\ndef evaluate(submission_path, assets_path):\n    assert os.environ['BRUNOST_ML_PRIVATE_LABELS'].endswith('labels.csv')\n    assert os.path.isfile(os.environ['BRUNOST_ML_PREDICTIONS_PATH'])\n    if os.environ.get('BRUNOST_ML_BASELINE_PREDICTIONS_PATH'):\n        assert os.path.isfile(os.environ['BRUNOST_ML_BASELINE_PREDICTIONS_PATH'])\n    return {'public': 0.25, 'private': 0.75}\n""",
        encoding="utf-8",
    )
    if baseline:
        (root / "private" / "baseline.py").write_text(
            "import os\nfrom pathlib import Path\nPath(os.environ['BRUNOST_ML_OUTPUT_PATH']).write_text('baseline\\n1\\n')\n",
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
        """import os\nfrom pathlib import Path\nfor _ in range(2000):\n    pass\nPath(os.environ['BRUNOST_ML_OUTPUT_PATH']).write_text('prediction\\n1\\n')\n""",
    )
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(0.75)
    assert r["metrics"]["public"] == pytest.approx(0.25)
    assert r["metrics"]["private"] == pytest.approx(0.75)


def test_run_model_training_submission_times_out(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(assets, """import time\ntime.sleep(1)\n""", training_ms=100)
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "failed"
    assert r["metrics"]["verdict"] == "time_limit_exceeded"


def test_run_model_training_submission_can_use_optional_baseline(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "import os\nfrom pathlib import Path\nPath(os.environ['BRUNOST_ML_OUTPUT_PATH']).write_text('prediction\\n1\\n')\n",
        baseline=True,
    )
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "completed", r
    assert r["score"] == pytest.approx(0.75)


def test_run_model_training_submission_rejects_output_escape(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_train_predict_task(
        assets,
        "import os\nfrom pathlib import Path\nPath(os.environ['BRUNOST_ML_OUTPUT_PATH']).write_text('prediction\\n1\\n')\n",
    )
    manifest = assets / "judge.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("prediction_output: predictions.csv", "prediction_output: ../escape.csv"),
        encoding="utf-8",
    )
    submission = assets.parent / "submission"
    r = run(str(submission), str(assets))
    assert r["status"] == "failed"
    assert "prediction output" in r["failure_reason"]


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
