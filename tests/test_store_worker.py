import json
import time
from pathlib import Path

import pytest

from brunost_judge.contracts import ExecutionRequest, ExecutionResult, TaskRecord
from brunost_judge.store import JudgeStore
from brunost_judge.worker import CallbackDispatcher, LocalWorker, RemoteWorker


def _store(tmp_path: Path) -> JudgeStore:
    task = tmp_path / "task"
    task.mkdir()
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("demo/v1", str(task), "ioai"))
    return store


def test_idempotent_execution_and_local_worker(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "public").mkdir()
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return {'public': 0.75}\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()

    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("task/v1", str(task), "ioai"))
    request = ExecutionRequest("task/v1", str(submission), "same-key")
    first = store.submit(request)
    second = store.submit(request)
    assert first.execution_id == second.execution_id
    result = LocalWorker(store).process_one()
    assert result is not None
    assert result.status == "completed"
    assert result.score == 0.75


def test_result_payload_has_stable_ids_and_preserves_score_metrics(tmp_path: Path):
    store = _store(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    execution = store.submit(ExecutionRequest("demo/v1", str(submission), "result-contract"))
    claimed = store.claim_next(worker_id="worker-result")
    assert claimed is not None

    finished = store.finish(
        execution.execution_id,
        ExecutionResult(
            execution.execution_id,
            "demo/v1",
            "completed",
            score=0.75,
            metrics={"public": 0.75, "tests": {"passed": 3}},
        ),
        worker_id="worker-result",
    )
    assert finished is not None
    payload = finished.as_dict()
    assert payload["execution_id"] == execution.execution_id
    assert payload["evaluation_id"] == execution.execution_id
    assert payload["event_id"] == f"execution:{execution.execution_id}:result"
    assert payload["score"] == 0.75
    assert payload["metrics"] == {"public": 0.75, "tests": {"passed": 3}}


def test_store_rejects_non_terminal_finish_results(tmp_path: Path):
    store = _store(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    execution = store.submit(ExecutionRequest("demo/v1", str(submission), "terminal-only"))
    assert store.claim_next(worker_id="worker-result") is not None

    with pytest.raises(ValueError, match="terminal"):
        store.finish(
            execution.execution_id,
            ExecutionResult(execution.execution_id, "demo/v1", "running"),
            worker_id="worker-result",
        )
    assert store.get_execution(execution.execution_id).status == "running"


def test_queued_cancel_is_terminal_and_enqueues_callback(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    execution = store.submit(
        ExecutionRequest(
            "demo/v1",
            str(submission),
            "queued-cancel",
            callback_url="http://callback.invalid/result",
        )
    )

    canceled = store.cancel(execution.execution_id)
    assert canceled is not None
    assert canceled.status == "canceled"
    pending = store.pending_callbacks()
    assert [row["execution_id"] for row in pending] == [execution.execution_id]

    calls = []
    monkeypatch.setattr("brunost_judge.worker._notify", lambda *args: calls.append(args))
    assert CallbackDispatcher(store, worker_id="callback-worker").deliver_callbacks() == 1
    assert len(calls) == 1
    assert calls[0][2]["status"] == "canceled"
    assert calls[0][2]["event_id"] == f"execution:{execution.execution_id}:result"
    assert store.pending_callbacks() == []


def test_legacy_result_gets_a_stable_event_id(tmp_path: Path):
    store = _store(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    execution = store.submit(ExecutionRequest("demo/v1", str(submission), "legacy-event"))

    with store._connect() as db:
        metadata = json.loads(db.execute(
            "SELECT metadata_json FROM executions WHERE execution_id=?",
            (execution.execution_id,),
        ).fetchone()[0])
        metadata.pop("event_id", None)
        db.execute(
            "UPDATE executions SET metadata_json=? WHERE execution_id=?",
            (json.dumps(metadata), execution.execution_id),
        )

    reloaded = store.get_execution(execution.execution_id)
    assert reloaded is not None
    assert reloaded.event_id == f"execution:{execution.execution_id}:result"
    assert reloaded.metadata["event_id"] == reloaded.event_id


def test_stale_callback_failure_cannot_clear_new_delivery_lease(tmp_path: Path):
    store = _store(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    execution = store.submit(
        ExecutionRequest(
            "demo/v1",
            str(submission),
            "callback-lease-fence",
            callback_url="http://callback.invalid/result",
        )
    )
    assert store.claim_next(worker_id="worker-result") is not None
    assert store.finish(
        execution.execution_id,
        ExecutionResult(execution.execution_id, "demo/v1", "completed", score=1.0),
        worker_id="worker-result",
    ) is not None
    assert store.claim_callback(execution.execution_id, "callback-a", lease_seconds=1)
    time.sleep(1.1)
    assert store.claim_callback(execution.execution_id, "callback-b", lease_seconds=60)

    assert not store.mark_callback_failed(execution.execution_id, "stale failure", worker_id="callback-a")
    row = store.pending_callbacks()[0]
    assert row["lease_owner"] == "callback-b"
    assert row["attempts"] == 0
    assert store.mark_callback_failed(execution.execution_id, "current failure", worker_id="callback-b")


def test_in_memory_store_is_shared_across_store_connections():
    store = JudgeStore(":memory:")
    assert store.list_tasks() == []


def test_callback_delivery_is_durable(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()
    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("task/v1", str(task), "ioai"))
    store.submit(ExecutionRequest("task/v1", str(submission), "callback-key", callback_url="http://callback.invalid"))

    calls = []
    monkeypatch.setattr("brunost_judge.worker._notify", lambda *args: calls.append(args))
    worker = LocalWorker(store)
    worker.process_one()
    assert calls
    assert store.pending_callbacks() == []


def test_remote_callback_failure_does_not_crash_worker(monkeypatch):
    worker = RemoteWorker.__new__(RemoteWorker)
    worker._pending_callbacks = []

    def fail(*_args):
        raise OSError("callback receiver unavailable")

    monkeypatch.setattr("brunost_judge.worker._notify", fail)
    payload = {"execution_id": "eval-1", "event_id": "execution:eval-1:result"}
    worker._send_callback("http://callback.invalid", None, payload)
    assert len(worker._pending_callbacks) == 1

    monkeypatch.setattr("brunost_judge.worker._notify", lambda *_args: None)
    worker._deliver_pending_callbacks()
    assert worker._pending_callbacks == []


def test_callback_dispatcher_retries_after_a_transient_store_failure(monkeypatch):
    dispatcher = CallbackDispatcher.__new__(CallbackDispatcher)
    dispatcher.poll_seconds = 0.1
    attempts = []
    sleeps = []

    def deliver_callbacks():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("database unavailable")
        raise KeyboardInterrupt

    dispatcher.deliver_callbacks = deliver_callbacks
    monkeypatch.setattr("brunost_judge.worker.time.sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(KeyboardInterrupt):
        dispatcher.run_forever()

    assert len(attempts) == 2
    assert sleeps == [0.5]
