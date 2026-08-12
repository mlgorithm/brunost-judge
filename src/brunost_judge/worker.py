"""Reference local worker.

This worker executes only task packages explicitly registered with the judge
store. Production deployments should place the same loop behind a hardened
container/microVM worker profile.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from pathlib import Path

from brunost_judge.contracts import ExecutionResult
from brunost_judge.security import callback_signature
from brunost_judge.store import JudgeStore
from grader.harness import run


def _notify(url: str, token: str | None, payload: dict, signing_secret: str | None = None) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "brunost-judge-worker/0.3"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if signing_secret:
        timestamp, signature = callback_signature(body, signing_secret)
        headers["X-Brunost-Judge-Timestamp"] = timestamp
        headers["X-Brunost-Judge-Signature"] = signature
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10):
        return


class LocalWorker:
    def __init__(
        self,
        store: JudgeStore,
        *,
        poll_seconds: float = 1.0,
        worker_id: str | None = None,
        queues: tuple[str, ...] | None = None,
        resource_classes: tuple[str, ...] | None = None,
        lease_seconds: int = 300,
        callback_signing_secret: str | None = None,
    ) -> None:
        self.store = store
        self.poll_seconds = poll_seconds
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.queues = queues
        self.resource_classes = resource_classes
        self.lease_seconds = lease_seconds
        self.callback_signing_secret = callback_signing_secret or os.environ.get("BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET")

    def process_one(self) -> ExecutionResult | None:
        claimed = self.store.claim_next(
            worker_id=self.worker_id,
            queues=self.queues,
            resource_classes=self.resource_classes,
            lease_seconds=self.lease_seconds,
        )
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
                queue=execution.queue,
                resource_class=execution.resource_class,
                priority=execution.priority,
            )
        except Exception as exc:  # noqa: BLE001 - worker must contain task failures
            result = ExecutionResult(
                execution_id=execution.execution_id,
                task_ref=execution.task_ref,
                status="failed",
                failure_reason=f"worker failure: {type(exc).__name__}: {exc}"[:2000],
                metadata=execution.metadata,
                queue=execution.queue,
                resource_class=execution.resource_class,
                priority=execution.priority,
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
                _notify(row["callback_url"], row["callback_token"], execution.as_dict(), self.callback_signing_secret)
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
