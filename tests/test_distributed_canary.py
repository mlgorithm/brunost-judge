from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

from brunost_judge.sandbox import ProcessSandboxRunner
from brunost_judge.sdk import JudgeClient
from brunost_judge.server import create_app
from brunost_judge.worker import RemoteWorker


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_api(app) -> tuple[str, uvicorn.Server, threading.Thread]:  # type: ignore[no-untyped-def]
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=0.5) as response:
                if response.status == 200:
                    return base_url, server, thread
        except OSError:
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("API did not start")


def _start_callback_receiver() -> tuple[ThreadingHTTPServer, list[tuple[bytes, dict[str, str]]], str]:
    received: list[tuple[bytes, dict[str, str]]] = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(size)
            headers = {key.lower(): value for key, value in self.headers.items()}
            received.append((body, headers))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    return receiver, received, f"http://127.0.0.1:{receiver.server_address[1]}/result"


def test_distributed_canary_covers_artifacts_callbacks_idempotency_and_lease_recovery(tmp_path: Path, monkeypatch):
    token = "canary-admin-token"
    callback_secret = "canary-callback-secret"
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", token)
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_API_TOKEN", "true")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN", "true")
    monkeypatch.setenv("BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS", "true")
    monkeypatch.setenv("BRUNOST_JUDGE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_MODE", "process")
    monkeypatch.setenv("BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET", callback_secret)
    monkeypatch.setenv("BRUNOST_JUDGE_LEASE_SECONDS", "1")

    callback_server, callbacks, callback_url = _start_callback_receiver()
    api_server = None
    api_thread = None
    try:
        app = create_app(tmp_path / "judge.db")
        base_url, api_server, api_thread = _start_api(app)
        admin = JudgeClient(base_url, token=token)

        lease_queue = "canary-lease"

        def enroll(
            worker_id: str,
            *,
            queues: list[str] | None = None,
            resource_classes: list[str] | None = None,
        ) -> tuple[str, str]:
            issued = admin.issue_enrollment_token(
                node_id=f"node-{worker_id}",
                worker_id=worker_id,
                queues=queues or ["default"],
                resource_classes=resource_classes or ["cpu"],
            )
            enrolled = JudgeClient(base_url).enroll_node(join_token=issued["join_token"])
            return worker_id, str(enrolled["worker_token"])

        worker_id, worker_token = enroll(
            "canary-worker-a",
            queues=["default", lease_queue],
            resource_classes=["cpu", lease_queue],
        )
        task_path = Path(__file__).parents[1] / "examples" / "ioi-sum"
        submission_path = Path(__file__).parents[1] / "examples" / "canary-ioi-sum"
        uploaded_task = admin.upload_artifact(task_path)
        task = admin.register_task(
            task_ref="canary/ioi-sum-v1",
            artifact_id=str(uploaded_task["artifact_id"]),
            kind="ioi",
        )
        uploaded_submission = admin.upload_artifact(submission_path)
        request = {
            "task_ref": "canary/ioi-sum-v1",
            "submission_artifact_id": str(uploaded_submission["artifact_id"]),
            "idempotency_key": "distributed-canary-1",
            "callback_url": callback_url,
            "queue": "default",
            "resource_class": "cpu",
            "metadata": {"canary": True},
        }
        first = admin.submit(**request)
        second = admin.submit(**request)
        assert first["execution_id"] == second["execution_id"]
        assert first["result_version"] == 1
        assert task["manifest"]["digest"]

        worker = RemoteWorker(
            base_url,
            worker_token,
            worker_id,
            sandbox_runner=ProcessSandboxRunner(),
        )
        result = worker.process_one()
        assert result is not None
        assert result.status == "completed"
        assert result.score == 1.0
        assert len(callbacks) == 1
        body, headers = callbacks[0]
        callback_payload = json.loads(body)
        assert callback_payload["execution_id"] == first["execution_id"]
        assert JudgeClient.verify_callback(
            body,
            secret=callback_secret,
            signature=headers["x-brunost-judge-signature"],
            timestamp=headers["x-brunost-judge-timestamp"],
            event_id=headers["x-brunost-judge-event-id"],
            require_event_id=True,
        )

        worker_b_id, worker_b_token = enroll(
            "canary-worker-b",
            queues=[lease_queue],
            resource_classes=[lease_queue],
        )
        lease_submission = admin.submit(
            task_ref="canary/ioi-sum-v1",
            submission_artifact_id=str(uploaded_submission["artifact_id"]),
            idempotency_key="distributed-canary-lease-1",
            queue=lease_queue,
            resource_class=lease_queue,
        )
        worker_a_client = JudgeClient(base_url, token=worker_token)
        worker_b_client = JudgeClient(base_url, token=worker_b_token)
        claimed_a = worker_a_client.claim_worker(worker_id)
        assert claimed_a["execution"]["execution_id"] == lease_submission["execution_id"]
        time.sleep(1.2)
        claimed_b = worker_b_client.claim_worker(worker_b_id)
        assert claimed_b["execution"]["execution_id"] == lease_submission["execution_id"]
        finished = worker_b_client.finish_worker(
            worker_b_id,
            {
                "execution_id": lease_submission["execution_id"],
                "task_ref": "canary/ioi-sum-v1",
                "status": "completed",
                "score": 1.0,
                "result_version": 1,
            },
        )
        assert finished["status"] == "completed"
        assert admin.health()["status"] == "ok"
        stats = admin.stats()
        assert stats["queued"] == 0
        assert stats["running"] == 0
    finally:
        callback_server.shutdown()
        callback_server.server_close()
        if api_server is not None:
            api_server.should_exit = True
        if api_thread is not None:
            api_thread.join(timeout=5)
