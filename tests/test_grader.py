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


def test_normalize_preserves_ioi_subtask_breakdown():
    """The IOI judge's structured metrics.subtasks rows must survive
    normalization verbatim — the API's per-subtask feedback depends on them."""
    subtasks = [
        {
            "id": 1, "name": "Subtask 1", "points": 30.0, "awarded": 30.0,
            "points_max": 30.0, "points_earned": 30.0, "verdict": "AC",
            "verdicts": ["s1a:AC"],
            "tests": [{"name": "s1a", "verdict": "AC", "time_ms": 12}],
        },
        {
            "id": 2, "name": "Subtask 2", "points": 70.0, "awarded": 0.0,
            "points_max": 70.0, "points_earned": 0.0, "verdict": "TLE",
            "first_failed_test": "s2a",
            "verdicts": ["s2a:TLE"],
            "tests": [{"name": "s2a", "verdict": "TLE", "time_ms": 1999}],
        },
    ]
    r = normalize_result({"public": 30.0, "private": 30.0, "metrics": {"subtasks": subtasks}})
    assert r["status"] == "completed"
    assert r["score"] == pytest.approx(30.0)
    assert r["metrics"]["subtasks"] == subtasks


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
