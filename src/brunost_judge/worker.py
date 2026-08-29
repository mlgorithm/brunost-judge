"""Reference local worker.

This worker executes only task packages explicitly registered with the judge
store. Production deployments should place the same loop behind a hardened
container/microVM worker profile.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from brunost_judge.artifacts import (
    ArtifactError,
    artifact_limits_from_environment,
    artifact_store_from_environment,
    safe_extract,
)
from brunost_judge.auth import configured_secret
from brunost_judge.conformance import validate_runner_result_payload
from brunost_judge.contracts import ExecutionResult, WorkerRecord
from brunost_judge.sandbox import SandboxRunner, sandbox_from_environment
from brunost_judge.sdk import JudgeClient
from brunost_judge.security import callback_signature
from brunost_judge.store import JudgeStore
from brunost_judge.task import task_digest

LOGGER = logging.getLogger(__name__)


@contextmanager
def _evaluation_profile_environment(metadata: dict[str, Any]):
    """Pass the selected ML evaluation profile into local or Docker sandboxes."""

    profile = str(metadata.get("evaluation_profile") or "live").strip().lower()
    previous = os.environ.get("BRUNOST_EVALUATION_PROFILE")
    os.environ["BRUNOST_EVALUATION_PROFILE"] = profile
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BRUNOST_EVALUATION_PROFILE", None)
        else:
            os.environ["BRUNOST_EVALUATION_PROFILE"] = previous


def _callback_secret_from_environment() -> str | None:
    secret = configured_secret("BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET")
    production = os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production", "staging"}
    required = production or os.environ.get("BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS", "false").lower() == "true"
    if required and not secret:
        raise RuntimeError("signed callbacks are required but no callback signing secret is configured")
    return secret


def _needs_plugin_runner(task_kind: str, metadata: dict) -> bool:
    return task_kind in {"agent", "game"} or str(metadata.get("evaluation_kind")) in {"agent", "match"}


def _stage_plugin_submission(
    submission: Path,
    *,
    execution_id: str,
    task_ref: str,
    task_kind: str,
    metadata: dict,
    stack: ExitStack,
    materialize,
) -> Path:  # type: ignore[no-untyped-def]
    """Build the read-only participant bundle consumed by ``runner.py``."""

    temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="brunost-plugin-"))
    root = Path(temporary)
    root.chmod(0o755)
    primary = root / "submission"
    shutil.copytree(submission, primary)
    participants: dict[str, str] = {}
    seats: list[dict[str, Any]] = []
    definitions = metadata.get("agent_definitions") or []
    if not isinstance(definitions, list):
        raise TypeError("agent_definitions must be a list")
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise TypeError("agent definition must be an object")
        agent_id = str(definition.get("agent_id") or "")
        artifact_path = definition.get("artifact_path")
        if not agent_id or not artifact_path:
            raise ValueError(f"agent {agent_id or '<unknown>'} has no immutable artifact")
        participant = materialize(str(artifact_path))
        if not participant.is_dir():
            raise ValueError(f"agent artifact is not a directory: {agent_id}")
        relative = Path("participants") / f"agent-{index}"
        shutil.copytree(participant, root / relative)
        participants[agent_id] = relative.as_posix()
        seat = {"agent_id": agent_id, "seat": index, "path": relative.as_posix()}
        if isinstance(definition.get("metadata"), dict):
            seat["metadata"] = dict(definition["metadata"])
            if isinstance(definition["metadata"].get("command"), (str, list)):
                seat["command"] = definition["metadata"]["command"]
        seats.append(seat)
    plugin_metadata = dict(metadata)
    plugin_metadata.pop("agent_definitions", None)
    manifest = {
        "version": 1,
        "execution_id": execution_id,
        "task_ref": task_ref,
        "task_kind": task_kind,
        "evaluation_kind": str(metadata.get("evaluation_kind") or ("match" if task_kind == "game" else "agent")),
        "participants": participants,
        "seats": seats,
        "seed": metadata.get("seed"),
        "metadata": plugin_metadata,
    }
    control = root / ".brunost"
    control.mkdir()
    control.joinpath("plugin.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return root


def _validated_runner_result(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise TypeError("sandbox returned a non-object result")
    errors = validate_runner_result_payload(raw)
    if errors:
        raise ValueError("invalid sandbox result: " + "; ".join(errors))
    return raw


def _execution_timeout(metadata: dict[str, Any], task_manifest: dict[str, Any]) -> int | None:
    """Return the strictest valid platform or task execution deadline.

    A classic task's wall-time estimate covers compilation and every test, so
    it must not be replaced by a longer client-supplied timeout. Older task
    records retain their former single-test ``time_limit_ms`` fallback.
    """

    values = [metadata.get("timeout_seconds"), task_manifest.get("execution_timeout_seconds")]
    if task_manifest.get("execution_timeout_seconds") is None:
        legacy_limit = task_manifest.get("time_limit_ms")
        try:
            values.append((int(legacy_limit) + 999) // 1_000 if legacy_limit is not None else None)
        except (TypeError, ValueError):
            values.append(None)
    limits: list[int] = []
    for value in values:
        if value is None:
            continue
        try:
            limits.append(max(1, min(86_400, int(value))))
        except (TypeError, ValueError):
            continue
    return min(limits) if limits else None


def _configured_sandbox(runner: SandboxRunner, timeout_seconds: int | None) -> SandboxRunner:
    configure = getattr(runner, "with_timeout", None)
    if callable(configure):
        return configure(timeout_seconds)
    return runner


@contextmanager
def _renew_execution_lease(renew, lease_seconds: int):  # type: ignore[no-untyped-def]
    """Keep a claimed execution alive while a blocking sandbox run is active."""

    stop = threading.Event()
    interval = max(0.25, min(30.0, max(1, lease_seconds) / 3))

    def heartbeat() -> None:
        while not stop.wait(interval):
            try:
                if not renew():
                    return
            except Exception as exc:  # noqa: BLE001 - the finish guard remains authoritative
                LOGGER.warning("could not renew execution lease: %s", exc)
                return

    thread = threading.Thread(target=heartbeat, name="brunost-lease-renewer", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(1.0, min(5.0, interval)))


def _persist_result_artifacts(payloads: object, put_artifact) -> dict[str, dict[str, Any]]:  # type: ignore[no-untyped-def]
    if not isinstance(payloads, dict):
        return {}
    references: dict[str, dict[str, Any]] = {}
    for name, payload in payloads.items():
        if not isinstance(name, str) or not isinstance(payload, dict) or not isinstance(payload.get("data"), bytes):
            raise TypeError(f"invalid result artifact payload: {name!r}")
        stored = put_artifact(payload["data"])
        reference = dict(stored)
        reference["name"] = name
        for key in ("media_type", "kind", "filename"):
            if payload.get(key) is not None:
                reference[key] = payload[key]
        references[name] = reference
    return references


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Callbacks must not turn an allowlisted URL into an SSRF redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "callback redirects are disabled", headers, fp)


_CALLBACK_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _notify(url: str, token: str | None, payload: dict, signing_secret: str | None = None) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "brunost-judge-worker/1.3.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if signing_secret:
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            raise ValueError("signed callbacks require a result event_id")
        timestamp, signature = callback_signature(body, signing_secret, event_id=event_id)
        headers["X-Brunost-Judge-Timestamp"] = timestamp
        headers["X-Brunost-Judge-Signature"] = signature
        headers["X-Brunost-Judge-Event-ID"] = event_id
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _CALLBACK_OPENER.open(request, timeout=10):
        return


def _deliver_callbacks(store: Any, worker_id: str, signing_secret: str | None, *, limit: int = 20) -> int:
    """Deliver terminal-result callbacks from the durable outbox."""

    delivered = 0
    for row in store.pending_callbacks(limit=limit):
        execution_id = str(row["execution_id"])
        if not store.claim_callback(execution_id, worker_id):
            continue
        execution = store.get_execution(execution_id)
        if execution is None:
            continue
        try:
            _notify(row["callback_url"], row["callback_token"], execution.as_dict(), signing_secret)
        except Exception as exc:  # noqa: BLE001 - retry delivery without re-execution
            store.mark_callback_failed(execution_id, f"{type(exc).__name__}: {exc}")
        else:
            store.mark_callback_delivered_by_owner(execution_id, worker_id)
            delivered += 1
    return delivered


class CallbackDispatcher:
    """Durable callback delivery loop for the Judge control plane."""

    def __init__(
        self,
        store: Any,
        *,
        poll_seconds: float = 1.0,
        worker_id: str | None = None,
        callback_signing_secret: str | None = None,
    ) -> None:
        self.store = store
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.worker_id = worker_id or f"callback-dispatcher-{uuid.uuid4().hex[:12]}"
        self.callback_signing_secret = callback_signing_secret or _callback_secret_from_environment()

    def deliver_callbacks(self) -> int:
        return _deliver_callbacks(self.store, self.worker_id, self.callback_signing_secret)

    def run_forever(self) -> None:
        while True:
            if self.deliver_callbacks() == 0:
                time.sleep(self.poll_seconds)


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
        sandbox_runner: SandboxRunner | None = None,
        capabilities: tuple[str, ...] = (),
        region: str | None = None,
    ) -> None:
        self.store = store
        self.poll_seconds = poll_seconds
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.queues = queues
        self.resource_classes = resource_classes
        self.lease_seconds = lease_seconds
        self.callback_signing_secret = callback_signing_secret or _callback_secret_from_environment()
        self.sandbox_runner = sandbox_runner or sandbox_from_environment()
        self.artifact_store = artifact_store_from_environment()
        self.require_immutable_artifacts = (
            os.environ.get("BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS", "false").lower() == "true"
            or os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production", "staging"}
        )
        advertised_capabilities = capabilities or tuple(
            value.strip() for value in os.environ.get("BRUNOST_JUDGE_CAPABILITIES", "runtime:local,resource:cpu").split(",") if value.strip()
        )
        self.capabilities = advertised_capabilities
        self.store.register_worker(WorkerRecord(
            worker_id=self.worker_id,
            capabilities=advertised_capabilities,
            queues=self.queues or ("default",),
            resource_classes=self.resource_classes or ("cpu",),
            region=region or os.environ.get("BRUNOST_JUDGE_REGION"),
        ))

    def process_one(self) -> ExecutionResult | None:
        self.store.heartbeat_worker(self.worker_id)
        claimed = self.store.claim_next(
            worker_id=self.worker_id,
            queues=self.queues,
            resource_classes=self.resource_classes,
            capabilities=self.capabilities,
            lease_seconds=self.lease_seconds,
        )
        if claimed is None:
            return None
        execution, task, context = claimed
        try:
            with ExitStack() as stack:
                if self.store.is_cancel_requested(execution.execution_id):
                    raw = {"status": "canceled", "score": 0.0, "metrics": {}, "failure_reason": "execution canceled before start"}
                else:
                    submission = self._materialize(context["submission_path"], stack)
                    task_path = self._materialize_task(task.path, task.manifest, stack)
                    if not submission.is_dir():
                        raise ValueError(f"submission path is not a directory: {submission}")
                    if _needs_plugin_runner(task.kind, execution.metadata):
                        submission = _stage_plugin_submission(
                            submission,
                            execution_id=execution.execution_id,
                            task_ref=execution.task_ref,
                            task_kind=task.kind,
                            metadata=execution.metadata,
                            stack=stack,
                            materialize=lambda value: self._materialize(value, stack),
                        )
                    runner = _configured_sandbox(self.sandbox_runner, _execution_timeout(execution.metadata, task.manifest))
                    with _renew_execution_lease(
                        lambda: self.store.renew_lease(
                            execution.execution_id,
                            self.worker_id,
                            lease_seconds=int(context.get("lease_seconds", self.lease_seconds)),
                        ),
                        int(context.get("lease_seconds", self.lease_seconds)),
                    ), _evaluation_profile_environment(execution.metadata):
                        raw = runner.run(submission, task_path, execution.execution_id)
                    if self.store.is_cancel_requested(execution.execution_id):
                        raw = {"status": "canceled", "score": 0.0, "metrics": {}, "failure_reason": "execution canceled while running"}
                    else:
                        raw = _validated_runner_result(raw)
                artifact_refs = _persist_result_artifacts(raw.pop("_artifact_payloads", {}), self.artifact_store.put) if raw.get("status") != "canceled" else {}
            result = ExecutionResult(
                execution_id=execution.execution_id,
                task_ref=execution.task_ref,
                status=raw.get("status", "failed"),
                score=raw.get("score"),
                metrics=raw.get("metrics") or {},
                scores=raw.get("scores") or {},
                winner=raw.get("winner"),
                artifacts=artifact_refs,
                failure_reason=raw.get("failure_reason"),
                metadata=execution.metadata,
                queue=execution.queue,
                resource_class=execution.resource_class,
                priority=execution.priority,
                task_digest=execution.task_digest,
                evaluator=execution.evaluator,
                runtime_image=execution.runtime_image,
                seed=execution.seed,
                event_id=execution.event_id,
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
                task_digest=execution.task_digest,
                evaluator=execution.evaluator,
                runtime_image=execution.runtime_image,
                seed=execution.seed,
                event_id=execution.event_id,
            )
        finished = self.store.finish(execution.execution_id, result, worker_id=self.worker_id)
        callback_url = context.get("callback_url")
        if finished is not None and callback_url:
            self.store.enqueue_callback(execution.execution_id, callback_url, context.get("callback_token"))
            self.deliver_callbacks()
        return finished

    def _materialize(self, value: str, stack: ExitStack) -> Path:
        if value.startswith("artifact://"):
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="brunost-local-artifact-"))
            # Sandbox containers run as an unprivileged UID.  TemporaryDirectory
            # defaults to 0700, which would make an otherwise valid artifact
            # invisible to that UID when the directory is bind-mounted.
            root = Path(temporary)
            root.chmod(0o755)
            return self.artifact_store.extract(self.artifact_store.get(value.removeprefix("artifact://")), root)
        if self.require_immutable_artifacts:
            raise ValueError("mutable filesystem paths are disabled; submit an artifact reference")
        return Path(value).expanduser().resolve()

    def _materialize_task(self, value: str, manifest: dict, stack: ExitStack) -> Path:
        task_path = self._materialize(value, stack)
        expected = str(manifest.get("digest") or "")
        if not expected:
            if self.require_immutable_artifacts:
                raise ValueError("task is missing an immutable digest")
            return task_path
        actual = task_digest(task_path)
        if actual != expected:
            raise ArtifactError(f"task digest mismatch: expected {expected}, got {actual}")
        return task_path

    def deliver_callbacks(self) -> int:
        return _deliver_callbacks(self.store, self.worker_id, self.callback_signing_secret)

    def run_forever(self) -> None:
        while True:
            self.deliver_callbacks()
            if self.process_one() is None:
                time.sleep(self.poll_seconds)


class RemoteWorker:
    """Worker agent that joins a remote control plane over HTTPS.

    Task and submission paths are deliberately mapped locally rather than
    copied implicitly.  Production deployments normally mount the same
    read-only task and submission roots on every worker or provide an object
    storage synchronizer around this agent.
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        worker_id: str,
        *,
        poll_seconds: float = 1.0,
        sandbox_runner: SandboxRunner | None = None,
        path_map: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.client = JudgeClient(api_url, token=token, timeout=30)
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self.sandbox_runner = sandbox_runner or sandbox_from_environment()
        self.path_map = path_map
        self.callback_signing_secret = _callback_secret_from_environment()
        self._pending_callbacks: list[tuple[str, str | None, dict, str | None, str | None]] = []
        self.require_immutable_artifacts = (
            os.environ.get("BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS", "false").lower() == "true"
            or os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production", "staging"}
        )

    def _local_path(self, value: str) -> Path:
        source = Path(value).expanduser()
        for remote_root, local_root in self.path_map:
            remote = Path(remote_root).expanduser()
            try:
                relative = source.relative_to(remote)
            except ValueError:
                continue
            return Path(local_root).expanduser() / relative
        return source

    def _materialize(self, value: str, stack: ExitStack) -> Path:
        if value.startswith("artifact://"):
            identifier = value.removeprefix("artifact://")
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="brunost-remote-artifact-"))
            # The Docker/gVisor sandbox evaluator needs to traverse
            # this bind-mounted extraction root.  Keep files read-only but make
            # the directory searchable by the sandbox user.
            root = Path(temporary)
            root.chmod(0o755)
            return safe_extract(
                self.client.download_artifact(self.worker_id, identifier),
                root,
                **artifact_limits_from_environment(),
            )
        if self.require_immutable_artifacts:
            raise ValueError("mutable filesystem paths are disabled; submit an artifact reference")
        return self._local_path(value)

    def _materialize_task(self, value: str, manifest: dict, stack: ExitStack) -> Path:
        task_path = self._materialize(value, stack)
        expected = str(manifest.get("digest") or "")
        if not expected:
            if self.require_immutable_artifacts:
                raise ValueError("task is missing an immutable digest")
            return task_path
        actual = task_digest(task_path)
        if actual != expected:
            raise ArtifactError(f"task digest mismatch: expected {expected}, got {actual}")
        return task_path

    def process_one(self) -> ExecutionResult | None:
        self._deliver_pending_callbacks()
        self.client.heartbeat_worker(self.worker_id)
        claimed = self.client.claim_worker(self.worker_id)
        if not claimed:
            return None
        execution_payload = claimed["execution"]
        task_payload = claimed["task"]
        context = claimed.get("context") or {}
        execution_id = execution_payload["execution_id"]
        try:
            with ExitStack() as stack:
                metadata = execution_payload.get("metadata") or {}
                task_manifest = task_payload.get("manifest") or {}
                if self.client.execution_cancel_requested(self.worker_id, execution_id):
                    raw = {"status": "canceled", "score": 0.0, "metrics": {}, "failure_reason": "execution canceled before start"}
                else:
                    submission = self._materialize(context["submission_path"], stack).resolve()
                    task_path = self._materialize_task(task_payload["path"], task_manifest, stack).resolve()
                    if not submission.is_dir():
                        raise ValueError(f"submission path is not a directory: {submission}")
                    if _needs_plugin_runner(str(task_payload.get("kind") or ""), metadata):
                        submission = _stage_plugin_submission(
                            submission,
                            execution_id=execution_id,
                            task_ref=str(execution_payload["task_ref"]),
                            task_kind=str(task_payload.get("kind") or ""),
                            metadata=metadata,
                            stack=stack,
                            materialize=lambda value: self._materialize(value, stack),
                        )
                    runner = _configured_sandbox(self.sandbox_runner, _execution_timeout(metadata, task_manifest))
                    lease_seconds = int(context.get("lease_seconds", 300))
                    with _renew_execution_lease(
                        lambda: self.client.renew_execution_lease(
                            self.worker_id,
                            execution_id,
                        ),
                        lease_seconds,
                    ), _evaluation_profile_environment(metadata):
                        raw = runner.run(submission, task_path, execution_id)
                    if self.client.execution_cancel_requested(self.worker_id, execution_id):
                        raw = {"status": "canceled", "score": 0.0, "metrics": {}, "failure_reason": "execution canceled while running"}
                    else:
                        raw = _validated_runner_result(raw)
                artifact_refs = _persist_result_artifacts(
                    raw.pop("_artifact_payloads", {}),
                    lambda data: self.client.upload_worker_artifact_bytes(self.worker_id, data),
                ) if raw.get("status") != "canceled" else {}
            result = ExecutionResult(
                execution_id=execution_id,
                task_ref=execution_payload["task_ref"],
                status=raw.get("status", "failed"),
                score=raw.get("score"),
                metrics=raw.get("metrics") or {},
                scores=raw.get("scores") or {},
                winner=raw.get("winner"),
                artifacts=artifact_refs,
                failure_reason=raw.get("failure_reason"),
                metadata=execution_payload.get("metadata") or {},
                queue=execution_payload.get("queue", "default"),
                resource_class=execution_payload.get("resource_class", "cpu"),
                priority=execution_payload.get("priority", 0),
                task_digest=execution_payload.get("task_digest"),
                evaluator=execution_payload.get("evaluator"),
                runtime_image=execution_payload.get("runtime_image"),
                seed=execution_payload.get("seed"),
                event_id=execution_payload.get("event_id"),
            )
        except Exception as exc:  # noqa: BLE001 - worker must contain task failures
            result = ExecutionResult(
                execution_id=execution_id,
                task_ref=execution_payload["task_ref"],
                status="failed",
                failure_reason=f"worker failure: {type(exc).__name__}: {exc}"[:2000],
                metadata=execution_payload.get("metadata") or {},
                queue=execution_payload.get("queue", "default"),
                resource_class=execution_payload.get("resource_class", "cpu"),
                priority=execution_payload.get("priority", 0),
                task_digest=execution_payload.get("task_digest"),
                evaluator=execution_payload.get("evaluator"),
                runtime_image=execution_payload.get("runtime_image"),
                seed=execution_payload.get("seed"),
                event_id=execution_payload.get("event_id"),
            )
        finished = self.client.finish_worker(self.worker_id, result.as_dict())
        callback_url = context.get("callback_url")
        if finished and callback_url:
            try:
                if self.client.claim_callback(self.worker_id, execution_id):
                    self._send_callback(callback_url, context.get("callback_token"), finished, execution_id=execution_id)
            except Exception as exc:  # noqa: BLE001 - the durable outbox will retry
                LOGGER.warning("could not claim callback for %s: %s", execution_id, exc)
        return result

    def _send_callback(self, callback_url: str, callback_token: str | None, payload: dict, *, execution_id: str | None = None) -> None:
        signing_secret = getattr(self, "callback_signing_secret", None)
        try:
            _notify(callback_url, callback_token, payload, signing_secret)
        except Exception as exc:  # noqa: BLE001 - retain the result and retry without crashing the worker
            LOGGER.warning("callback delivery failed for %s: %s", payload.get("execution_id"), exc)
            self._pending_callbacks.append((callback_url, callback_token, payload, signing_secret, execution_id))
        else:
            if execution_id is not None:
                try:
                    self.client.acknowledge_callback(self.worker_id, execution_id)
                except Exception as exc:  # noqa: BLE001 - delivery succeeded; lease expiry permits a safe retry
                    LOGGER.warning("callback acknowledgement failed for %s: %s", payload.get("execution_id"), exc)

    def _deliver_pending_callbacks(self) -> None:
        if not self._pending_callbacks:
            return
        pending, self._pending_callbacks = self._pending_callbacks, []
        for callback_url, callback_token, payload, signing_secret, execution_id in pending:
            try:
                _notify(callback_url, callback_token, payload, signing_secret)
            except Exception as exc:  # noqa: BLE001 - leave the item queued for the next poll
                LOGGER.warning("callback retry failed for %s: %s", payload.get("execution_id"), exc)
                self._pending_callbacks.append((callback_url, callback_token, payload, signing_secret, execution_id))
            else:
                if execution_id is not None:
                    try:
                        self.client.acknowledge_callback(self.worker_id, execution_id)
                    except Exception as exc:  # noqa: BLE001 - delivery succeeded; lease expiry permits a safe retry
                        LOGGER.warning("callback acknowledgement retry failed for %s: %s", payload.get("execution_id"), exc)

    def run_forever(self) -> None:
        while True:
            if self.process_one() is None:
                time.sleep(self.poll_seconds)
