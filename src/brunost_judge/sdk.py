"""Dependency-free HTTP SDK for the standalone judge API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from brunost_judge.security import verify_callback_signature


class JudgeAPIError(RuntimeError):
    """An HTTP request to the judge API failed."""


class JudgeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8787", token: str | None = None, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise JudgeAPIError(f"judge API {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise JudgeAPIError(f"judge API unavailable: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def register_task(self, *, task_ref: str, path: str) -> dict[str, Any]:
        return self._request("POST", "/v1/tasks", {"task_ref": task_ref, "path": path})

    def submit(
        self,
        *,
        task_ref: str,
        submission_path: str,
        idempotency_key: str,
        callback_url: str | None = None,
        callback_token: str | None = None,
        metadata: dict[str, Any] | None = None,
        queue: str = "default",
        resource_class: str = "cpu",
        priority: int = 0,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/executions", {
            "task_ref": task_ref,
            "submission_path": submission_path,
            "idempotency_key": idempotency_key,
            "callback_url": callback_url,
            "callback_token": callback_token,
            "metadata": metadata or {},
            "queue": queue,
            "resource_class": resource_class,
            "priority": priority,
        })

    @staticmethod
    def verify_callback(payload: bytes, *, secret: str, signature: str, timestamp: str) -> bool:
        """Verify the signed callback headers emitted by a worker."""
        return verify_callback_signature(payload, secret, signature, timestamp)

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/executions/{execution_id}")

    def cancel(self, execution_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/executions/{execution_id}/cancel")
