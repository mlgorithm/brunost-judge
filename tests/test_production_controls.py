from pathlib import Path

from brunost_judge.contracts import ExecutionRequest, TaskRecord
from brunost_judge.plugins import RunnerContext, RunnerRegistry
from brunost_judge.security import callback_signature, verify_callback_signature
from brunost_judge.store import JudgeStore


def _store(tmp_path: Path) -> JudgeStore:
    task = tmp_path / "task"
    task.mkdir()
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("demo/v1", str(task), "ioai"))
    return store


def test_priority_queue_and_worker_lease(tmp_path: Path):
    store = _store(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    store.submit(ExecutionRequest("demo/v1", str(submission), "low", queue="cpu", priority=1))
    store.submit(ExecutionRequest("demo/v1", str(submission), "high", queue="cpu", priority=10))
    claimed = store.claim_next(worker_id="cpu-1", queues=("cpu",), lease_seconds=60)
    assert claimed is not None
    assert claimed[0].metadata["event_id"].startswith("execution:")
    assert claimed[2]["queue"] == "cpu"
    assert store.claim_next(worker_id="gpu-1", queues=("gpu",)) is None


def test_worker_claim_filters_required_capabilities(tmp_path: Path):
    store = _store(tmp_path)
    store.register_task(
        TaskRecord(
            "gpu/v1",
            str(tmp_path / "task"),
            "ioai",
            {"required_capabilities": ["gpu:true"]},
        )
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    store.submit(ExecutionRequest("gpu/v1", str(submission), "gpu-key"))
    assert store.claim_next(worker_id="cpu-1", capabilities=("resource:cpu",)) is None
    claimed = store.claim_next(worker_id="gpu-1", capabilities=("resource:cpu", "gpu:true"))
    assert claimed is not None
    assert claimed[0].task_ref == "gpu/v1"


def test_callback_signature_rejects_stale_payload():
    body = b'{"status":"completed"}'
    timestamp, signature = callback_signature(body, "secret", "100")
    assert not verify_callback_signature(body, "secret", signature, timestamp)
    current_timestamp, current_signature = callback_signature(body, "secret")
    assert verify_callback_signature(body, "secret", current_signature, current_timestamp)
    assert not verify_callback_signature(body + b"x", "secret", current_signature, current_timestamp)


def test_callback_signature_binds_event_id():
    body = b'{"status":"completed"}'
    timestamp, signature = callback_signature(body, "secret", event_id="execution:1:result")
    assert verify_callback_signature(
        body,
        "secret",
        signature,
        timestamp,
        event_id="execution:1:result",
        require_event_id=True,
    )
    assert not verify_callback_signature(
        body,
        "secret",
        signature,
        timestamp,
        event_id="execution:2:result",
        require_event_id=True,
    )
    assert not verify_callback_signature(body, "secret", signature, timestamp, require_event_id=True)


def test_runner_registry_validates_and_replaces_plugins():
    class Plugin:
        name = "test-plugin"
        version = "1"
        kinds = frozenset({"agent"})

        def run(self, context):
            return {"status": "completed", "score": 1.0, "metrics": {"id": context.execution_id}}

    registry = RunnerRegistry()
    registry.register(Plugin())
    result = registry.run("agent", RunnerContext("e-1", "agent/v1", "agent", "/task", "/submission"))
    assert result["metrics"] == {"id": "e-1"}
    assert registry.names() == {"agent": "test-plugin"}
