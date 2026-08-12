from pathlib import Path

from brunost_judge.contracts import ExecutionRequest, TaskRecord
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
