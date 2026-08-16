import threading
from pathlib import Path

from brunost_judge.contracts import ExecutionRequest, TaskRecord
from brunost_judge.sandbox import ProcessSandboxRunner
from brunost_judge.store import JudgeStore
from brunost_judge.worker import LocalWorker


def _task(root: Path, *, kind: str = "ioai", runner: str = "def evaluate(s, a): return 1.0\n") -> Path:
    task = root / "task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "judge.yaml").write_text(f"version: 1\nkind: {kind}\nrunner: python\n", encoding="utf-8")
    if kind in {"agent", "game"}:
        (task / "runner.py").write_text(runner, encoding="utf-8")
    else:
        (task / "scorer" / "metrics.py").write_text(runner, encoding="utf-8")
    return task


def test_plugin_timeout_becomes_a_structured_failure(tmp_path: Path):
    task = _task(
        tmp_path,
        kind="agent",
        runner="import time\n\ndef run(context):\n    time.sleep(2)\n    return {'status': 'completed', 'score': 1.0}\n",
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("agent/timeout", str(task), "agent", {"kind": "agent"}))
    store.submit(ExecutionRequest("agent/timeout", str(submission), "timeout-1", timeout_seconds=1))

    result = LocalWorker(store, sandbox_runner=ProcessSandboxRunner()).process_one()

    assert result is not None
    assert result.status == "failed"
    assert "timed out after 1s" in (result.failure_reason or "")


def test_running_cancellation_is_reflected_after_sandbox_boundary(tmp_path: Path):
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("cancel/v1", str(task), "ioai"))
    submitted = store.submit(ExecutionRequest("cancel/v1", str(submission), "cancel-1"))
    started = threading.Event()
    release = threading.Event()

    class BlockingRunner:
        def run(self, submission_path: Path, task_path: Path, execution_id: str) -> dict:
            _ = submission_path, task_path, execution_id
            started.set()
            release.wait(timeout=5)
            return {"status": "completed", "score": 1.0, "metrics": {}}

    result_holder: list = []

    def execute() -> None:
        result_holder.append(LocalWorker(store, sandbox_runner=BlockingRunner()).process_one())

    thread = threading.Thread(target=execute)
    thread.start()
    assert started.wait(timeout=2)
    store.cancel(submitted.execution_id)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result_holder[0] is not None
    assert result_holder[0].status == "canceled"
    assert store.get_execution(submitted.execution_id).status == "canceled"
