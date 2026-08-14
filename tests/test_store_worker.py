from pathlib import Path

from brunost_judge.contracts import ExecutionRequest, TaskRecord
from brunost_judge.store import JudgeStore
from brunost_judge.worker import LocalWorker, RemoteWorker


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
