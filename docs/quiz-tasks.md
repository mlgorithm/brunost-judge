# Quiz tasks

Quiz tasks are deterministic, answer-file evaluations. They do not execute
participant code. The answer key is private to the Judge and the participant
uploads one bounded JSON file.

## Manifest

```yaml
version: 1
kind: quiz
runner: quiz
answer_key: private/questions.json
submission_file: answers.json
scoring_mode: weighted # or all_or_nothing
free_text_normalization: casefold_trim
network: disabled
```

The validator accepts at most 500 questions and a 4 MiB answer key. Question
and choice identifiers must be short safe strings. Each question has a
positive finite `points` value; the total is limited to 1,000,000 points.
Prompts and choice text are optional metadata in the private key, so authors
may publish a contestant-visible copy under `public/` without exposing the
answers.

## Answer key

`private/questions.json` contains an object with a `questions` array. Each
question has a unique `id`, a `type`, and optional `prompt` and `points`:

```json
{
  "questions": [
    {
      "id": "capital",
      "type": "single_choice",
      "prompt": "What is the capital of Norway?",
      "choices": [
        {"id": "a", "text": "Bergen"},
        {"id": "b", "text": "Oslo"}
      ],
      "answer": "b",
      "points": 1
    },
    {
      "id": "languages",
      "type": "multiple_choice",
      "choices": [
        {"id": "python", "text": "Python"},
        {"id": "rust", "text": "Rust"},
        {"id": "html", "text": "HTML"}
      ],
      "answer": ["python", "rust"],
      "points": 2
    },
    {
      "id": "short-answer",
      "type": "free_text",
      "accepted_answers": ["Oslo", "the city of Oslo"],
      "points": 1
    }
  ]
}
```

Single-choice answers are one choice id. Multiple-choice answers are an exact
set of unique choice ids; there is no accidental partial credit. Free-text
answers match one of `accepted_answers` after the manifest's normalization.
The default `casefold_trim` makes matching case-insensitive and ignores outer
whitespace. `exact`, `trim`, `collapse_whitespace`, and
`casefold_collapse_whitespace` are also supported.

## Participant submission

The default `answers.json` must contain exactly one top-level field:

```json
{"answers": {"capital": "b", "languages": ["python", "rust"], "short-answer": "Oslo"}}
```

The submission is limited to 1 MiB. Missing answers are simply incorrect;
unknown question ids, malformed answer types, invalid JSON, and oversized
submissions receive a completed zero-score `INVALID_SUBMISSION` verdict.

## Scoring and results

`weighted` divides awarded points by total points. `all_or_nothing` reports
one only when every question is correct. Results include aggregate counts,
points, scoring configuration, and per-question correctness/points metadata,
but never include the answer key or the participant's raw answers.

