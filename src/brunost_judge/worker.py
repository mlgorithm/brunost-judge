"""Reference local worker.

This worker executes only task packages explicitly registered with the judge
store. Production deployments should place the same loop behind a hardened
container/microVM worker profile.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from brunost_judge.contracts import ExecutionResult
from brunost_judge.store import JudgeStore
from grader.harness import run


def _notify(url: str, token: str | None, payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10):
        return


class LocalWorker:
    def __init__(self, store: JudgeStore, *, poll_seconds: float = 1.0) -> None:
        self.store = store
        self.poll_seconds = poll_seconds

    def process_one(self) -> ExecutionResult | None:
        claimed = self.store.claim_next()
        if claimed is None:
            return None
        execution, task, context = claimed
        try:
            submission = Path(context["submission_path"]).expanduser().resolve()
            if not submission.is_dir():
                raise ValueError(f"submission path is not a directory: {submission}")
            raw = run(str(submission), task.path)
            result = ExecutionResult(
                execution_id=execution.execution_id,
                task_ref=execution.task_ref,
                status=raw.get("status", "failed"),
                score=raw.get("score"),
                metrics=raw.get("metrics") or {},
                failure_reason=raw.get("failure_reason"),
                metadata=execution.metadata,
            )
        except Exception as exc:  # noqa: BLE001 - worker must contain task failures
            result = ExecutionResult(
                execution_id=execution.execution_id,
                task_ref=execution.task_ref,
                status="failed",
                failure_reason=f"worker failure: {type(exc).__name__}: {exc}"[:2000],
                metadata=execution.metadata,
            )
        finished = self.store.finish(execution.execution_id, result)
        callback_url = context.get("callback_url")
        if callback_url:
            self.store.enqueue_callback(execution.execution_id, callback_url, context.get("callback_token"))
            self.deliver_callbacks()
        return finished

    def deliver_callbacks(self) -> int:
        delivered = 0
        for row in self.store.pending_callbacks():
            execution = self.store.get_execution(row["execution_id"])
            if execution is None:
                continue
            try:
                _notify(row["callback_url"], row["callback_token"], execution.as_dict())
            except Exception as exc:  # noqa: BLE001 - retry delivery without re-execution
                self.store.mark_callback_failed(row["execution_id"], f"{type(exc).__name__}: {exc}")
            else:
                self.store.mark_callback_delivered(row["execution_id"])
                delivered += 1
        return delivered

    def run_forever(self) -> None:
        while True:
            self.deliver_callbacks()
            if self.process_one() is None:
                time.sleep(self.poll_seconds)
