from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brunost_judge.server import create_app
from brunost_judge.task import scaffold_task, validate_task
from grader.harness import run


def _quiz_task(root: Path, *, scoring_mode: str = "weighted", normalization: str = "casefold_trim") -> Path:
    task = root / "task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\n"
        "kind: quiz\n"
        "runner: quiz\n"
        "answer_key: private/questions.json\n"
        f"scoring_mode: {scoring_mode}\n"
        f"free_text_normalization: {normalization}\n"
        "network: disabled\n",
        encoding="utf-8",
    )
    (task / "private" / "questions.json").write_text(
        json.dumps(
            {
                "title": "Example quiz",
                "questions": [
                    {
                        "id": "single",
                        "type": "single_choice",
                        "choices": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                        "answer": "b",
                        "points": 1,
                    },
                    {
                        "id": "multiple",
                        "type": "multiple_choice",
                        "choices": [
                            {"id": "a", "text": "A"},
                            {"id": "b", "text": "B"},
                            {"id": "c", "text": "C"},
                        ],
                        "answer": ["a", "c"],
                        "points": 2,
                    },
                    {
                        "id": "text",
                        "type": "free_text",
                        "accepted_answers": ["Oslo", "the city of Oslo"],
                        "points": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return task


def _submission(root: Path, answers: object) -> Path:
    submission = root / "submission"
    submission.mkdir(parents=True)
    (submission / "answers.json").write_text(json.dumps({"answers": answers}), encoding="utf-8")
    return submission


def test_quiz_validation_and_runner_support_all_question_types(tmp_path: Path):
    task = _quiz_task(tmp_path)
    validation = validate_task(task)
    assert validation.valid, validation.errors
    assert validation.settings["evaluator"] == "grader.quiz:run_quiz"
    assert validation.settings["runtime"] == "python-3.13"

    result = run(
        str(_submission(tmp_path, {"single": "b", "multiple": ["c", "a"], "text": "  OSLO  "})),
        str(task),
    )
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0)
    assert result["metrics"]["verdict"] == "AC"
    assert result["metrics"]["correct_questions"] == 3
    assert result["metrics"]["points_earned"] == pytest.approx(4)
    assert all("answer" not in row for row in result["metrics"]["questions"])


def test_quiz_weighted_scoring_is_exact_for_multiple_choice_and_missing_answers(tmp_path: Path):
    task = _quiz_task(tmp_path)
    result = run(
        str(_submission(tmp_path, {"single": "b", "multiple": ["a"], "text": "wrong"})),
        str(task),
    )
    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(0.25)
    assert result["metrics"]["correct_questions"] == 1
    assert result["metrics"]["answered_questions"] == 3

    missing = run(str(_submission(tmp_path / "missing", {"single": "b"})), str(task))
    assert missing["score"] == pytest.approx(0.25)
    assert missing["metrics"]["answered_questions"] == 1


def test_quiz_all_or_nothing_and_invalid_submission_are_explicit(tmp_path: Path):
    task = _quiz_task(tmp_path, scoring_mode="all_or_nothing")
    wrong = run(
        str(_submission(tmp_path, {"single": "b", "multiple": ["a"], "text": "Oslo"})),
        str(task),
    )
    assert wrong["status"] == "completed"
    assert wrong["score"] == 0.0
    assert wrong["metrics"]["verdict"] == "PARTIAL"

    malformed = run(
        str(_submission(tmp_path / "malformed", {"single": ["b"], "extra": "x"})),
        str(task),
    )
    assert malformed["status"] == "completed"
    assert malformed["score"] == 0.0
    assert malformed["metrics"]["verdict"] == "INVALID_SUBMISSION"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda payload: payload["questions"][0].update({"answer": "missing"}), "answer must name"),
        (lambda payload: payload["questions"].append(payload["questions"][0].copy()), "duplicate quiz question id"),
    ],
)
def test_quiz_validation_rejects_invalid_answer_keys(tmp_path: Path, mutate, expected: str):
    task = _quiz_task(tmp_path)
    payload = json.loads((task / "private" / "questions.json").read_text(encoding="utf-8"))
    mutate(payload)
    (task / "private" / "questions.json").write_text(json.dumps(payload), encoding="utf-8")
    validation = validate_task(task)
    assert not validation.valid
    assert any(expected in error for error in validation.errors), validation.errors


def test_quiz_scaffold_is_valid_and_contains_private_key(tmp_path: Path):
    task = scaffold_task(tmp_path / "scaffold", "quiz")
    validation = validate_task(task)
    assert validation.valid, validation.errors
    assert (task / "private" / "questions.json").is_file()


def test_quiz_registers_as_a_builtin_batch_task(tmp_path: Path):
    task = _quiz_task(tmp_path)
    client = TestClient(create_app(tmp_path / "judge.db"))
    response = client.post("/v1/tasks", json={"task_ref": "quiz/v1", "path": str(task)})
    assert response.status_code == 201, response.text
    manifest = response.json()["manifest"]
    assert manifest["kind"] == "quiz"
    assert manifest["evaluator"] == "grader.quiz:run_quiz"
    assert manifest["runtime"] == "python-3.13"
