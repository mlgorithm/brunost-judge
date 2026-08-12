from pathlib import Path

from grader.harness import run


def test_example_scores(tmp_path: Path):
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "answer.txt").write_text("brunost\n", encoding="utf-8")
    result = run(str(submission), str(Path(__file__).parents[1]))
    assert result["status"] == "completed"
    assert result["score"] == 1.0
