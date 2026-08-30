from __future__ import annotations

from pathlib import Path

import pytest

from brunost_judge.task import scaffold_task, validate_task
from grader.classic import ClassicConfig, _compile
from grader.harness import run


def _optimization_task(
    root: Path,
    *,
    score_mode: str = "checker_score",
    baseline: bool = False,
    direction: str = "maximize",
    time_limit_ms: int = 1000,
    output_limit_bytes: int = 65536,
    evaluator_source: str | None = None,
) -> Path:
    task = root / "task"
    (task / "public" / "instances").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\n"
        "kind: optimization\n"
        "runner: optimization\n"
        "language: python\n"
        f"time_limit_ms: {time_limit_ms}\n"
        "memory_limit_mb: 256\n"
        f"output_limit_bytes: {output_limit_bytes}\n"
        "network: disabled\n"
        "evaluation: evaluator:evaluate\n"
        f"objective_direction: {direction}\n"
        f"score_mode: {score_mode}\n"
        "aggregation: mean\n"
        "evaluator_entrypoint: private/evaluator.py\n"
        f"baseline_enabled: {'true' if baseline else 'false'}\n"
        + ("baseline_entrypoint: private/baseline.py\n" if baseline else ""),
        encoding="utf-8",
    )
    (task / "private" / "evaluator.py").write_text(
        evaluator_source
        or (
            "from pathlib import Path\n\n"
            "def evaluate(input_path, output_path):\n"
            "    capacity = int(Path(input_path).read_text())\n"
            "    value = int(Path(output_path).read_text())\n"
            "    return {'feasible': 0 <= value <= capacity, 'objective': value, 'score': value / capacity}\n"
        ),
        encoding="utf-8",
    )
    if baseline:
        (task / "private" / "baseline.py").write_text(
            "import sys\nprint(sys.stdin.read().strip())\n", encoding="utf-8"
        )
    for name, value in (("one", "10\n"), ("two", "20\n")):
        (task / "tests" / f"{name}.in").write_text(value, encoding="utf-8")
    return task


def _submission(root: Path, source: str) -> Path:
    submission = root / "submission"
    submission.mkdir(parents=True)
    (submission / "solution.py").write_text(source, encoding="utf-8")
    return submission


def test_optimization_runner_scores_evaluator_outputs_and_rejects_infeasible_candidates(tmp_path):
    task = _optimization_task(tmp_path)
    validation = validate_task(task)
    assert validation.valid, validation.errors

    result = run(
        str(_submission(tmp_path, "import sys\nprint(int(sys.stdin.read()))\n")),
        str(task),
    )
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0)
    assert result["metrics"]["runner"] == "optimization"
    assert result["metrics"]["verdict"] == "OK"
    assert result["metrics"]["feasible_tests"] == 2

    infeasible = run(
        str(_submission(tmp_path / "infeasible", "import sys\nprint(int(sys.stdin.read()) + 1)\n")),
        str(task),
    )
    assert infeasible["status"] == "completed"
    assert infeasible["score"] == 0.0
    assert infeasible["metrics"]["verdict"] == "INFEASIBLE"
    assert all(row["verdict"] == "INFEASIBLE" for row in infeasible["metrics"]["tests"])


def test_optimization_runner_normalizes_objectives_against_a_baseline(tmp_path):
    task = _optimization_task(tmp_path, score_mode="baseline_ratio", baseline=True)
    validation = validate_task(task)
    assert validation.valid, validation.errors

    result = run(
        str(_submission(tmp_path, "import sys\nprint(int(sys.stdin.read()) // 2)\n")),
        str(task),
    )
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(0.5)
    assert result["metrics"]["score_mode"] == "baseline_ratio"
    assert [row["score"] for row in result["metrics"]["tests"]] == pytest.approx([0.5, 0.5])


def test_optimization_runner_normalizes_minimization_against_a_baseline(tmp_path):
    task = _optimization_task(
        tmp_path,
        score_mode="baseline_ratio",
        baseline=True,
        direction="minimize",
        evaluator_source=(
            "from pathlib import Path\n\n"
            "def evaluate(input_path, output_path):\n"
            "    capacity = int(Path(input_path).read_text())\n"
            "    value = int(Path(output_path).read_text())\n"
            "    return {'feasible': 0 < value <= capacity * 2, 'objective': value}\n"
        ),
    )
    validation = validate_task(task)
    assert validation.valid, validation.errors

    result = run(
        str(_submission(tmp_path, "import sys\nprint(int(sys.stdin.read()) * 2)\n")),
        str(task),
    )
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(0.5)
    assert result["metrics"]["objective_direction"] == "minimize"


def test_optimization_runner_classifies_explicitly_invalid_candidate_output(tmp_path):
    task = _optimization_task(
        tmp_path,
        evaluator_source=(
            "from pathlib import Path\n"
            "from grader.optimization import InvalidOptimizationOutput\n\n"
            "def evaluate(input_path, output_path):\n"
            "    try:\n"
            "        value = int(Path(output_path).read_text().strip())\n"
            "    except ValueError as exc:\n"
            "        raise InvalidOptimizationOutput('output must be an integer') from exc\n"
            "    return {'feasible': value >= 0, 'objective': value, 'score': 1.0}\n"
        ),
    )
    result = run(str(_submission(tmp_path, "print('not-a-number')\n")), str(task))
    assert result["status"] == "completed", result
    assert result["score"] == 0.0
    assert result["metrics"]["verdict"] == "INVALID"
    assert result["metrics"]["tests"][0]["message"] == "output must be an integer"


def test_optimization_runner_keeps_broken_evaluators_as_task_errors(tmp_path):
    task = _optimization_task(
        tmp_path,
        evaluator_source="def evaluate(input_path, output_path):\n    raise RuntimeError('broken evaluator')\n",
    )
    result = run(str(_submission(tmp_path, "print(1)\n")), str(task))
    assert result["status"] == "failed"
    assert "broken evaluator" in result["failure_reason"]


def test_optimization_runner_bounds_evaluator_time(tmp_path):
    task = _optimization_task(
        tmp_path,
        time_limit_ms=100,
        evaluator_source="import time\n\ndef evaluate(input_path, output_path):\n    time.sleep(2)\n    return {'feasible': True, 'objective': 1, 'score': 1}\n",
    )
    result = run(str(_submission(tmp_path, "print(1)\n")), str(task))
    assert result["status"] == "failed"
    assert "exceeded its time limit" in result["failure_reason"]


@pytest.mark.parametrize(
    ("source", "expected_verdict", "time_limit_ms", "output_limit_bytes"),
    [
        ("while True: pass\n", "TLE", 100, 1024),
        ("print('x' * 100000)\n", "OLE", 1000, 1024),
    ],
)
def test_optimization_runner_reports_candidate_resource_verdicts(
    tmp_path, source, expected_verdict, time_limit_ms, output_limit_bytes
):
    task = _optimization_task(tmp_path, time_limit_ms=time_limit_ms, output_limit_bytes=output_limit_bytes)
    result = run(str(_submission(tmp_path, source)), str(task))
    assert result["status"] == "completed", result
    assert result["metrics"]["verdict"] == expected_verdict
    assert all(row["verdict"] == expected_verdict for row in result["metrics"]["tests"])
    assert result["metrics"]["limits"] == {
        "time_limit_ms": time_limit_ms,
        "memory_limit_mb": 256,
        "output_limit_bytes": output_limit_bytes,
    }


def test_optimization_validation_requires_baseline_for_ratio_scoring(tmp_path):
    task = _optimization_task(tmp_path, score_mode="baseline_ratio", baseline=False)
    validation = validate_task(task)
    assert not validation.valid
    assert "baseline_ratio scoring requires baseline_enabled: true" in validation.errors


def test_optimization_does_not_run_stale_baseline_when_disabled(tmp_path):
    task = _optimization_task(tmp_path)
    (task / "private" / "baseline.py").write_text(
        "raise RuntimeError('disabled baseline must not run')\n", encoding="utf-8"
    )
    manifest = (task / "judge.yaml").read_text(encoding="utf-8")
    (task / "judge.yaml").write_text(manifest + "baseline_entrypoint: private/baseline.py\n", encoding="utf-8")

    result = run(str(_submission(tmp_path, "import sys\nprint(int(sys.stdin.read()))\n")), str(task))
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0)


def test_optimization_scaffold_is_runnable(tmp_path):
    task = scaffold_task(tmp_path / "scaffold", "optimization")
    validation = validate_task(task)
    assert validation.valid, validation.errors
    result = run(
        str(_submission(tmp_path, "import sys\nprint(sys.stdin.read().strip())\n")),
        str(task),
    )
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0)


def test_optimization_stages_private_submission_source_for_contestant_user(tmp_path):
    submission = _submission(tmp_path, "print(1)\n")
    source = submission / "solution.py"
    source.chmod(0o600)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _compile(
        source,
        ClassicConfig(
            kind="optimization",
            language="python",
            entrypoint=None,
            interactor="",
            time_limit_ms=1000,
            memory_limit_mb=256,
            output_limit_bytes=65536,
            answer_source="answer_key",
            scoring_mode="percentage",
            reference_language="python",
            reference_entrypoint=None,
        ),
        build_dir,
    )
    assert (build_dir / "solution.py").stat().st_mode & 0o777 == 0o644
