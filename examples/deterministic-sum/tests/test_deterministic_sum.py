from pathlib import Path

from brunost_judge.task import validate_task
from grader.harness import run

TASK = Path(__file__).parents[1]


def _submission(tmp_path: Path, source: str) -> Path:
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(source, encoding="utf-8")
    return submission


def test_example_package_is_valid_and_accepts_the_reference_solution(tmp_path: Path):
    validation = validate_task(TASK)
    assert validation.valid, validation.errors
    submission = _submission(
        tmp_path,
        "import sys\nprint(int(sys.stdin.read()) * 2)\n",
    )

    result = run(str(submission), str(TASK))

    assert result["status"] == "completed", result
    assert result["score"] == 1.0
    assert result["metrics"]["verdict"] == "AC"


def test_example_rejects_an_incorrect_solution(tmp_path: Path):
    submission = _submission(tmp_path, "print(0)\n")

    result = run(str(submission), str(TASK))

    assert result["status"] == "completed", result
    assert result["score"] == 0.0
    assert result["metrics"]["verdict"] == "WA"

