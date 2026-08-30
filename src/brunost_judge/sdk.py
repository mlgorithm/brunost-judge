"""Dependency-free HTTP SDK for the standalone judge API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from brunost_judge.auth import configured_secret
from brunost_judge.security import verify_callback_signature
from brunost_judge.transport import (
    DEFAULT_MAX_ARTIFACT_RESPONSE_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseTooLarge,
    SafeHttpTransport,
)


class JudgeAPIError(RuntimeError):
    """An HTTP request to the judge API failed."""


def _segment(value: str, label: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{label} is required")
    return quote(value, safe="")


class JudgeClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        token: str | None = None,
        timeout: float = 30,
        *,
        ca_file: str | None = None,
        client_cert_file: str | None = None,
        client_key_file: str | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_artifact_response_bytes: int = DEFAULT_MAX_ARTIFACT_RESPONSE_BYTES,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Judge URL must be an absolute HTTP(S) URL")
        production = os.environ.get("BRUNOST_JUDGE_ENV", os.environ.get("ENVIRONMENT", "")).lower() in {
            "prod",
            "production",
            "staging",
        }
        if production and parsed.scheme != "https":
            allow_internal = os.environ.get("BRUNOST_JUDGE_ALLOW_INSECURE_HTTP", "false").lower() == "true"
            internal_hosts = {
                item.strip().lower()
                for item in os.environ.get("BRUNOST_JUDGE_INTERNAL_HTTP_HOSTS", "").split(",")
                if item.strip()
            }
            if not (allow_internal and parsed.hostname and parsed.hostname.lower() in internal_hosts):
                raise ValueError("Judge URL must use HTTPS outside development")
        if timeout <= 0:
            raise ValueError("Judge timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else configured_secret("BRUNOST_JUDGE_API_TOKEN")
        self.timeout = timeout
        self._transport = SafeHttpTransport(
            ca_file=ca_file,
            client_cert_file=client_cert_file,
            client_key_file=client_key_file,
            max_response_bytes=max_response_bytes,
            max_artifact_response_bytes=max_artifact_response_bytes,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if path in {"/v1/executions", "/v1/evaluations"} and payload and payload.get("idempotency_key"):
            headers["Idempotency-Key"] = str(payload["idempotency_key"])
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self._transport.open(request, timeout=self.timeout) as response:
                body = self._transport.read_json(response).decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = self._transport.read(exc, max_bytes=64 * 1024).decode(errors="replace")
            except ResponseTooLarge:
                detail = "response body too large"
            raise JudgeAPIError(f"judge API {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ResponseTooLarge) as exc:
            raise JudgeAPIError(f"judge API unavailable: {getattr(exc, 'reason', exc)}") from exc

    def _raw(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        *,
        content_type: str = "application/octet-stream",
        artifact_response: bool = False,
    ) -> bytes:
        headers = {"Accept": "application/octet-stream", "Content-Type": content_type}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self._transport.open(request, timeout=self.timeout) as response:
                return (
                    self._transport.read_artifact(response)
                    if artifact_response
                    else self._transport.read_json(response)
                )
        except urllib.error.HTTPError as exc:
            try:
                detail = self._transport.read(exc, max_bytes=64 * 1024).decode(errors="replace")
            except ResponseTooLarge:
                detail = "response body too large"
            raise JudgeAPIError(f"judge API {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ResponseTooLarge) as exc:
            raise JudgeAPIError(f"judge API unavailable: {getattr(exc, 'reason', exc)}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def create_service_credential(
        self,
        *,
        name: str,
        scopes: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "scopes": scopes or ["judge:read", "judge:write"]}
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        return self._request("POST", "/v1/auth/service-credentials", payload)

    def revoke_service_credential(self, credential_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/auth/service-credentials/{_segment(credential_id, 'credential ID')}/revoke")

    def rotate_admin_token(self) -> dict[str, Any]:
        return self._request("POST", "/v1/auth/admin-token/rotate")

    def audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/audit?limit={max(1, min(1000, int(limit)))}")  # type: ignore[return-value]

    def register_task(
        self,
        *,
        task_ref: str,
        path: str | None = None,
        artifact_id: str | None = None,
        kind: str | None = None,
        version: int | None = None,
        runtime: str | None = None,
        evaluator: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"task_ref": task_ref}
        if path:
            payload["path"] = path
        if artifact_id:
            payload["artifact_id"] = artifact_id
        if kind:
            payload["kind"] = kind
        if version is not None:
            payload["version"] = version
        if runtime:
            payload["runtime"] = runtime
        if evaluator:
            payload["evaluator"] = evaluator
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", "/v1/tasks", payload)

    def upload_artifact(self, path: str | Path, *, artifact_id: str | None = None) -> dict[str, Any]:
        from brunost_judge.artifacts import artifact_id as digest_artifact
        from brunost_judge.artifacts import pack_directory

        data = pack_directory(path)
        identifier = artifact_id or digest_artifact(data)
        return self.upload_artifact_bytes(data, artifact_id=identifier)

    def upload_artifact_bytes(self, data: bytes, *, artifact_id: str | None = None) -> dict[str, Any]:
        from brunost_judge.artifacts import artifact_id as digest_artifact

        identifier = artifact_id or digest_artifact(data)
        return self._request_json_bytes("PUT", f"/v1/artifacts/{_segment(identifier, 'artifact ID')}", data)

    def upload_worker_artifact_bytes(self, worker_id: str, data: bytes, *, artifact_id: str | None = None) -> dict[str, Any]:
        from brunost_judge.artifacts import artifact_id as digest_artifact

        identifier = artifact_id or digest_artifact(data)
        return self._request_json_bytes("PUT", f"/v1/workers/{_segment(worker_id, 'worker ID')}/artifacts/{_segment(identifier, 'artifact ID')}", data)

    def _request_json_bytes(self, method: str, path: str, data: bytes) -> dict[str, Any]:
        response = self._raw(method, path, data, content_type="application/gzip")
        return json.loads(response.decode())

    def download_artifact(self, worker_id: str, identifier: str) -> bytes:
        return self._raw(
            "GET",
            f"/v1/workers/{_segment(worker_id, 'worker ID')}/artifacts/{_segment(identifier, 'artifact ID')}",
            artifact_response=True,
        )

    def download_result_artifact(self, identifier: str) -> bytes:
        return self._raw("GET", f"/v1/artifacts/{_segment(identifier, 'artifact ID')}", artifact_response=True)

    def register_agent(
        self,
        *,
        agent_id: str,
        name: str,
        version: str = "1",
        artifact_path: str | None = None,
        artifact_id: str | None = None,
        protocol: str = "stdio",
        resource_profile: dict[str, Any] | None = None,
        required_capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/agents", {
            "agent_id": agent_id,
            "name": name,
            "version": version,
            "artifact_path": artifact_path,
            "artifact_id": artifact_id,
            "protocol": protocol,
            "resource_profile": resource_profile or {},
            "required_capabilities": required_capabilities or [],
            "metadata": metadata or {},
        })

    def list_agents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/agents")  # type: ignore[return-value]

    def register_game(
        self,
        *,
        game_id: str,
        name: str,
        task_ref: str,
        seats: int = 2,
        protocol: str = "stdio",
        referee: str | None = None,
        resource_profile: dict[str, Any] | None = None,
        required_capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/games", {
            "game_id": game_id,
            "name": name,
            "task_ref": task_ref,
            "seats": seats,
            "protocol": protocol,
            "referee": referee,
            "resource_profile": resource_profile or {},
            "required_capabilities": required_capabilities or [],
            "metadata": metadata or {},
        })

    def list_games(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/games")  # type: ignore[return-value]

    def register_worker(
        self,
        *,
        worker_id: str,
        capabilities: list[str] | None = None,
        queues: list[str] | None = None,
        resource_classes: list[str] | None = None,
        region: str | None = None,
        status: str = "ready",
        draining: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/workers/register", {
            "worker_id": worker_id,
            "capabilities": capabilities or [],
            "queues": queues or ["default"],
            "resource_classes": resource_classes or ["cpu"],
            "region": region,
            "status": status,
            "draining": draining,
            "metadata": metadata or {},
        })

    def advertise_worker_capabilities(
        self,
        worker_id: str,
        capabilities: list[str],
    ) -> dict[str, Any]:
        """Refresh the runtime inventory for this worker's scoped credential."""

        return self._request(
            "POST",
            f"/v1/workers/{_segment(worker_id, 'worker ID')}/capabilities",
            {"capabilities": list(dict.fromkeys(capabilities))},
        )

    def list_workers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/workers")  # type: ignore[return-value]

    def issue_enrollment_token(
        self,
        *,
        node_id: str,
        worker_id: str | None = None,
        role: str = "worker",
        capabilities: list[str] | None = None,
        queues: list[str] | None = None,
        resource_classes: list[str] | None = None,
        region: str | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/nodes/enrollment-tokens", {
            "node_id": node_id,
            "worker_id": worker_id,
            "role": role,
            "capabilities": capabilities or [],
            "queues": queues or ["default"],
            "resource_classes": resource_classes or ["cpu"],
            "region": region,
            "metadata": metadata or {},
            "ttl_seconds": ttl_seconds,
        })

    def enroll_node(
        self,
        *,
        join_token: str,
        hostname: str | None = None,
        capabilities: list[str] | None = None,
        resource_classes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "join_token": join_token,
            "hostname": hostname,
            "metadata": metadata or {},
        }
        if capabilities is not None:
            payload["capabilities"] = capabilities
        if resource_classes is not None:
            payload["resource_classes"] = resource_classes
        return self._request("POST", "/v1/nodes/enroll", payload)

    def heartbeat_worker(self, worker_id: str, *, status: str = "ready") -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/heartbeat?status={quote(status, safe='')}")

    def claim_callback(self, worker_id: str, execution_id: str) -> bool:
        payload = self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/callbacks/{_segment(execution_id, 'execution ID')}/claim")
        return bool(payload.get("claimed"))

    def acknowledge_callback(self, worker_id: str, execution_id: str) -> bool:
        payload = self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/callbacks/{_segment(execution_id, 'execution ID')}/ack")
        return bool(payload.get("delivered"))

    def worker_status(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{_segment(worker_id, 'worker ID')}/status")

    def execution_cancel_requested(self, worker_id: str, execution_id: str) -> bool:
        payload = self._request("GET", f"/v1/workers/{_segment(worker_id, 'worker ID')}/executions/{_segment(execution_id, 'execution ID')}/cancel-requested")
        return bool(payload.get("cancel_requested"))

    def renew_execution_lease(self, worker_id: str, execution_id: str) -> bool:
        payload = self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/executions/{_segment(execution_id, 'execution ID')}/lease")
        return bool(payload.get("renewed"))

    def claim_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/claim")

    def finish_worker(self, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/finish", result)

    def drain_worker(self, worker_id: str, *, draining: bool = True) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/drain?draining={'true' if draining else 'false'}")

    def revoke_worker_credential(self, worker_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{_segment(worker_id, 'worker ID')}/credential/revoke")

    def submit(
        self,
        *,
        task_ref: str,
        submission_path: str | None = None,
        submission_artifact_id: str | None = None,
        idempotency_key: str,
        callback_url: str | None = None,
        callback_token: str | None = None,
        metadata: dict[str, Any] | None = None,
        queue: str = "default",
        resource_class: str = "cpu",
        priority: int = 0,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "task_ref": task_ref,
            "submission_path": submission_path,
            "submission_artifact_id": submission_artifact_id,
            "idempotency_key": idempotency_key,
            "callback_url": callback_url,
            "callback_token": callback_token,
            "metadata": metadata or {},
            "queue": queue,
            "resource_class": resource_class,
            "priority": priority,
            "timeout_seconds": timeout_seconds,
        }
        return self._request("POST", "/v1/executions", payload)

    def submit_directory(self, *, task_ref: str, submission_path: str | Path, idempotency_key: str, **kwargs: Any) -> dict[str, Any]:
        uploaded = self.upload_artifact(submission_path)
        return self.submit(task_ref=task_ref, submission_artifact_id=str(uploaded["artifact_id"]), idempotency_key=idempotency_key, **kwargs)

    @staticmethod
    def verify_callback(
        payload: bytes,
        *,
        secret: str,
        signature: str,
        timestamp: str,
        event_id: str | None = None,
        require_event_id: bool = False,
    ) -> bool:
        """Verify the signed callback headers emitted by a worker."""
        return verify_callback_signature(payload, secret, signature, timestamp, event_id=event_id, require_event_id=require_event_id)

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/executions/{_segment(execution_id, 'execution ID')}")

    def stats(self) -> dict[str, int]:
        return self._request("GET", "/v1/stats")  # type: ignore[return-value]

    def submit_evaluation(self, **kwargs: Any) -> dict[str, Any]:
        """Submit using the canonical evaluation resource name."""
        return self._request("POST", "/v1/evaluations", kwargs)

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/evaluations/{_segment(evaluation_id, 'evaluation ID')}")

    def submit_match(self, game_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", f"/v1/games/{_segment(game_id, 'game ID')}/matches", kwargs)

    def cancel(self, execution_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/executions/{_segment(execution_id, 'execution ID')}/cancel")

    def replay_callback(self, execution_id: str) -> dict[str, Any]:
        """Ask the durable callback dispatcher to send a terminal result again."""
        return self._request("POST", f"/v1/executions/{_segment(execution_id, 'execution ID')}/callback/replay")
