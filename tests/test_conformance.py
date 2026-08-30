import pytest

from brunost_judge.conformance import (
    assert_conformant,
    validate_capability_payload,
    validate_result_payload,
    validate_runner_result_payload,
)


def test_result_and_capability_conformance():
    result = {
        "evaluation_id": "eval-1",
        "task_ref": "ioai/v1",
        "status": "completed",
        "score": 0.5,
        "metrics": {},
        "result_version": 1,
    }
    capabilities = {"worker_id": "worker-1", "capabilities": ["resource:cpu"]}
    assert validate_result_payload(result) == ()
    assert validate_capability_payload(capabilities) == ()
    assert_conformant(result, kind="result")
    assert_conformant(capabilities, kind="capabilities")


def test_conformance_reports_invalid_plugin_payloads():
    errors = validate_result_payload({"status": "unknown", "metrics": []})
    assert "missing evaluation_id" in errors
    assert "status must be queued, running, completed, failed, or canceled" in errors
    with pytest.raises(ValueError, match="capabilities must be"):
        assert_conformant({"worker_id": "worker-1", "capabilities": "cpu"}, kind="capabilities")


def test_conformance_rejects_non_finite_and_invalid_sandbox_results():
    assert "score must be finite" in validate_result_payload(
        {"evaluation_id": "eval-1", "task_ref": "task/v1", "status": "completed", "score": float("nan")}
    )
    assert "score must be finite" in validate_result_payload(
        {"evaluation_id": "eval-1", "task_ref": "task/v1", "status": "completed", "score": 10**1000}
    )
    assert validate_runner_result_payload({"status": "completed", "score": 1.0, "metrics": {}}) == ()
    assert "sandbox result score must be numeric or null" in validate_runner_result_payload(
        {"status": "completed", "score": True, "metrics": {}}
    )


def test_conformance_validates_match_scores_and_artifacts():
    assert validate_runner_result_payload(
        {
            "status": "completed",
            "score": 1.0,
            "scores": {"red": 1.0, "blue": 0.0},
            "winner": "red",
            "artifacts": {"replay": {"path": "replay.jsonl", "kind": "replay"}},
            "metrics": {},
        }
    ) == ()

    errors = validate_runner_result_payload(
        {
            "status": "completed",
            "score": 1.0,
            "scores": {"red": float("inf")},
            "artifacts": {"replay": {"path": "../secret"}},
            "metrics": {},
        }
    )
    assert "sandbox result scores values must be finite" in errors
    assert "sandbox result artifacts.replay path must be relative" in errors
    assert validate_result_payload(
        {
            "execution_id": "eval-1",
            "task_ref": "game/v1",
            "status": "completed",
            "score": 1.0,
            "scores": {"red": 1.0},
            "artifacts": {"replay": {"artifact_id": "a" * 64, "size_bytes": 10}},
            "metrics": {},
        }
    ) == ()


def test_conformance_rejects_oversized_metrics_and_non_content_addressed_artifacts():
    errors = validate_result_payload({
        "execution_id": "e-1",
        "task_ref": "task/v1",
        "status": "completed",
        "metrics": {"trace": "x" * 1_000_001},
        "artifacts": {"replay": {"artifact_id": "not-a-sha256"}},
    })

    assert "metrics exceeds 1000000 bytes" in errors
    assert "artifacts.replay must declare an artifact_id" in errors
