import pytest

from brunost_judge.conformance import (
    assert_conformant,
    validate_capability_payload,
    validate_result_payload,
)


def test_result_and_capability_conformance():
    result = {"evaluation_id": "eval-1", "task_ref": "ioai/v1", "status": "completed", "score": 0.5, "metrics": {}}
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
