"""Deterministic quiz scorer.

Quiz task packages keep their answer key in ``private/questions.json``. A
contestant submits one JSON file (``answers.json`` by default):
``{"answers": {"question-id": answer}}``. The runner never executes
contestant code, applies strict bounds, and returns only scoring metadata (it
never includes the expected answers in a result).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_DIAGNOSTIC_CHARS = 4000
MAX_QUIZ_QUESTIONS = 500
MAX_QUIZ_CHOICES = 100
MAX_QUIZ_TEXT_CHARS = 20_000
MAX_QUIZ_ANSWER_CHARS = 4_096
MAX_QUIZ_ANSWERS_PER_TEXT = 100
MAX_QUIZ_KEY_BYTES = 4 * 1024 * 1024
MAX_QUIZ_SUBMISSION_BYTES = 1 * 1024 * 1024
MAX_QUIZ_POINTS = 1_000_000
QUIZ_TYPES = frozenset({"single_choice", "multiple_choice", "free_text"})
QUIZ_TEXT_NORMALIZATIONS = frozenset(
    {"exact", "trim", "casefold_trim", "collapse_whitespace", "casefold_collapse_whitespace"}
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


class QuizJudgeError(ValueError):
    """Raised for a broken task package or an unsupported quiz contract."""


@dataclass(frozen=True)
class QuizQuestion:
    question_id: str
    question_type: str
    points: float
    choices: frozenset[str]
    accepted_answers: tuple[str, ...]


@dataclass(frozen=True)
class QuizConfig:
    answer_key: str
    submission_file: str
    scoring_mode: str
    text_normalization: str
    questions: tuple[QuizQuestion, ...]


def _failed(reason: str, *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "failed",
        "score": 0.0,
        "metrics": metrics or {"runner": "quiz"},
        "failure_reason": reason[:MAX_DIAGNOSTIC_CHARS],
    }


def _invalid_submission(reason: str, *, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "completed",
        "score": 0.0,
        "metrics": {"runner": "quiz", "verdict": "INVALID_SUBMISSION", **metrics},
        "failure_reason": reason[:MAX_DIAGNOSTIC_CHARS],
    }


def _manifest(task: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = (task / "judge.yaml").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QuizJudgeError(f"cannot read judge.yaml: {exc}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not key.startswith("-"):
            result[key] = value
    return result


def _safe_relative(root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or "\\" in value or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise QuizJudgeError(f"{label} must stay inside its bundle")
    resolved = (root / candidate).resolve()
    if resolved == root or root not in resolved.parents:
        raise QuizJudgeError(f"{label} must stay inside its bundle")
    return resolved


def _bounded_json(path: Path, label: str, limit: int) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise QuizJudgeError(f"could not inspect {label}: {exc}") from exc
    if size > limit:
        raise QuizJudgeError(f"{label} exceeds {limit} bytes")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        if len(raw) > limit:
            raise ValueError(f"file grew beyond {limit} bytes")
        return json.loads(
            raw.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise QuizJudgeError(f"{label} is not valid JSON: {exc}") from exc


def _text(value: Any, label: str, *, max_chars: int = MAX_QUIZ_TEXT_CHARS) -> str:
    if not isinstance(value, str) or len(value) > max_chars:
        raise QuizJudgeError(f"{label} must be text of at most {max_chars} characters")
    return value


def _load_config(task: Path) -> QuizConfig:
    values = _manifest(task)
    if values.get("kind", "").lower() != "quiz":
        raise QuizJudgeError("quiz runner requires kind: quiz")
    if values.get("version") != "1":
        raise QuizJudgeError("quiz tasks must declare version: 1")
    if values.get("runner", "").lower() != "quiz":
        raise QuizJudgeError("quiz tasks must declare runner: quiz")
    answer_key = values.get("answer_key", "private/questions.json")
    answer_path = _safe_relative(task, answer_key, "answer_key")
    private_root = (task / "private").resolve()
    if private_root != answer_path and private_root not in answer_path.parents:
        raise QuizJudgeError("answer_key must stay under private/")
    if not answer_path.is_file():
        raise QuizJudgeError(f"quiz answer key does not exist: {answer_key}")
    scoring_mode = values.get("scoring_mode", "weighted").lower()
    if scoring_mode not in {"weighted", "all_or_nothing"}:
        raise QuizJudgeError("quiz scoring_mode must be weighted or all_or_nothing")
    normalization = values.get("free_text_normalization", "casefold_trim").lower()
    if normalization not in QUIZ_TEXT_NORMALIZATIONS:
        raise QuizJudgeError("unsupported free_text_normalization")
    submission_file = values.get("submission_file", "answers.json")
    if not submission_file or Path(submission_file).is_absolute() or ".." in Path(submission_file).parts:
        raise QuizJudgeError("submission_file must be a relative path without parent traversal")

    payload = _bounded_json(answer_path, "quiz answer_key", MAX_QUIZ_KEY_BYTES)
    if not isinstance(payload, dict) or set(payload) - {"questions", "title", "description"}:
        raise QuizJudgeError("quiz answer_key must contain only questions, title, and description")
    questions_data = payload.get("questions")
    if not isinstance(questions_data, list) or not 1 <= len(questions_data) <= MAX_QUIZ_QUESTIONS:
        raise QuizJudgeError(f"quiz answer_key questions must contain 1 to {MAX_QUIZ_QUESTIONS} items")

    questions: list[QuizQuestion] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(questions_data, start=1):
        label = f"quiz question {index}"
        if not isinstance(raw, dict):
            raise QuizJudgeError(f"{label} must be an object")
        if set(raw) - {"id", "type", "prompt", "choices", "points", "answer", "accepted_answers"}:
            raise QuizJudgeError(f"{label} contains unsupported fields")
        question_id = raw.get("id")
        if not isinstance(question_id, str) or not _ID_RE.fullmatch(question_id) or question_id in seen_ids:
            raise QuizJudgeError(f"{label} id must be unique and safe")
        seen_ids.add(question_id)
        question_type = raw.get("type")
        if question_type not in QUIZ_TYPES:
            raise QuizJudgeError(f"{label} type is unsupported")
        if "prompt" in raw:
            _text(raw["prompt"], f"{label} prompt")
        points = raw.get("points", 1)
        if isinstance(points, bool) or not isinstance(points, (int, float)) or not math.isfinite(float(points)) or not 0 < points <= MAX_QUIZ_POINTS:
            raise QuizJudgeError(f"{label} points must be a finite number in (0, {MAX_QUIZ_POINTS}]")

        choices: frozenset[str] = frozenset()
        if question_type in {"single_choice", "multiple_choice"}:
            raw_choices = raw.get("choices")
            if not isinstance(raw_choices, list) or not 2 <= len(raw_choices) <= MAX_QUIZ_CHOICES:
                raise QuizJudgeError(f"{label} choices must contain 2 to {MAX_QUIZ_CHOICES} items")
            choice_ids: set[str] = set()
            for choice in raw_choices:
                if not isinstance(choice, dict) or set(choice) != {"id", "text"}:
                    raise QuizJudgeError(f"{label} choices must contain only id and text")
                choice_id = choice.get("id")
                if not isinstance(choice_id, str) or not _ID_RE.fullmatch(choice_id) or choice_id in choice_ids:
                    raise QuizJudgeError(f"{label} contains an invalid or duplicate choice id")
                _text(choice["text"], f"{label} choice text")
                choice_ids.add(choice_id)
            choices = frozenset(choice_ids)
        elif "choices" in raw:
            raise QuizJudgeError(f"{label} free_text questions must not declare choices")

        if question_type == "single_choice":
            accepted: tuple[str, ...] = (raw.get("answer"),) if isinstance(raw.get("answer"), str) else ()
            if len(accepted) != 1 or accepted[0] not in choices:
                raise QuizJudgeError(f"{label} answer must name one choice")
        elif question_type == "multiple_choice":
            raw_answer = raw.get("answer")
            if (
                not isinstance(raw_answer, list)
                or not raw_answer
                or len(raw_answer) > MAX_QUIZ_CHOICES
                or any(not isinstance(item, str) or item not in choices for item in raw_answer)
                or len(set(raw_answer)) != len(raw_answer)
            ):
                raise QuizJudgeError(f"{label} answer must be a non-empty list of unique choice ids")
            accepted = tuple(sorted(raw_answer))
        else:
            raw_accepted = raw.get("accepted_answers", raw.get("answer"))
            if isinstance(raw_accepted, str):
                raw_accepted = [raw_accepted]
            if (
                not isinstance(raw_accepted, list)
                or not 1 <= len(raw_accepted) <= MAX_QUIZ_ANSWERS_PER_TEXT
                or any(not isinstance(item, str) or len(item) > MAX_QUIZ_ANSWER_CHARS for item in raw_accepted)
            ):
                raise QuizJudgeError(f"{label} accepted_answers are invalid")
            accepted = tuple(raw_accepted)
        questions.append(QuizQuestion(question_id, question_type, float(points), choices, accepted))

    return QuizConfig(answer_key, submission_file, scoring_mode, normalization, tuple(questions))


def _normalize_text(value: str, mode: str) -> str:
    if mode == "exact":
        return value
    if mode == "trim":
        return value.strip()
    if mode == "casefold_trim":
        return value.strip().casefold()
    collapsed = " ".join(value.split())
    return collapsed.casefold() if mode == "casefold_collapse_whitespace" else collapsed


def _is_correct(question: QuizQuestion, answer: Any, normalization: str) -> tuple[bool, bool]:
    if answer is None:
        return False, False
    if question.question_type == "single_choice":
        if not isinstance(answer, str):
            raise ValueError("single_choice answers must be strings")
        return answer == question.accepted_answers[0], True
    if question.question_type == "multiple_choice":
        if (
            not isinstance(answer, list)
            or not answer
            or len(answer) > MAX_QUIZ_CHOICES
            or any(not isinstance(item, str) or item not in question.choices for item in answer)
            or len(set(answer)) != len(answer)
        ):
            raise ValueError("multiple_choice answers must be a list of unique choice ids")
        return set(answer) == set(question.accepted_answers), True
    if not isinstance(answer, str) or len(answer) > MAX_QUIZ_ANSWER_CHARS:
        raise ValueError("free_text answers must be short strings")
    normalized = _normalize_text(answer, normalization)
    accepted = {_normalize_text(item, normalization) for item in question.accepted_answers}
    return normalized in accepted, True


def run_quiz(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Score a quiz submission; malformed contestant answers receive zero."""

    try:
        task = Path(assets_path).resolve()
        submission = Path(submission_path).resolve()
        if not task.is_dir() or not submission.is_dir():
            raise QuizJudgeError("task and submission must be directories")
        config = _load_config(task)
        answer_path = _safe_relative(submission, config.submission_file, "submission_file")
        if not answer_path.is_file():
            return _invalid_submission(
                f"submission is missing {config.submission_file}",
                metrics={"total_questions": len(config.questions), "answered_questions": 0},
            )
        try:
            payload = _bounded_json(answer_path, "quiz submission", MAX_QUIZ_SUBMISSION_BYTES)
        except QuizJudgeError as exc:
            return _invalid_submission(
                str(exc),
                metrics={"total_questions": len(config.questions), "answered_questions": 0},
            )
        if not isinstance(payload, dict) or set(payload) != {"answers"} or not isinstance(payload["answers"], dict):
            return _invalid_submission(
                'submission must be an object with exactly one "answers" object',
                metrics={"total_questions": len(config.questions), "answered_questions": 0},
            )
        answers = payload["answers"]
        question_map = {question.question_id: question for question in config.questions}
        unknown = sorted(set(answers) - set(question_map))
        if unknown:
            return _invalid_submission(
                "submission contains unknown question ids",
                metrics={"total_questions": len(config.questions), "answered_questions": 0},
            )

        rows: list[dict[str, Any]] = []
        correct_count = 0
        answered_count = 0
        earned = 0.0
        for question in config.questions:
            try:
                correct, answered = _is_correct(question, answers.get(question.question_id), config.text_normalization)
            except ValueError as exc:
                return _invalid_submission(
                    str(exc),
                    metrics={"total_questions": len(config.questions), "answered_questions": answered_count},
                )
            awarded = question.points if correct else 0.0
            correct_count += int(correct)
            answered_count += int(answered)
            earned += awarded
            rows.append(
                {
                    "id": question.question_id,
                    "type": question.question_type,
                    "points": question.points,
                    "awarded_points": awarded,
                    "answered": answered,
                    "correct": correct,
                }
            )

        available = sum(question.points for question in config.questions)
        if config.scoring_mode == "all_or_nothing" and correct_count != len(config.questions):
            earned = 0.0
        score = earned / available if available else 0.0
        return {
            "status": "completed",
            "score": round(score, 8),
            "metrics": {
                "runner": "quiz",
                "verdict": "AC" if correct_count == len(config.questions) else "PARTIAL",
                "scoring_mode": config.scoring_mode,
                "free_text_normalization": config.text_normalization,
                "correct_questions": correct_count,
                "answered_questions": answered_count,
                "total_questions": len(config.questions),
                "points_earned": round(earned, 8),
                "points_available": round(available, 8),
                "questions": rows,
            },
        }
    except QuizJudgeError as exc:
        return _failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - scorer failures must be terminal and explicit
        return _failed(f"quiz judge failure: {type(exc).__name__}: {exc}")
