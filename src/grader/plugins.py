"""Versioned evaluator-plugin contract used inside the judge sandbox.

The module is dependency-free so it can be copied into the evaluator image.
Task packages provide a ``runner.py`` with a ``run(context)`` function; image
authors may register additional trusted plugins through ``RunnerRegistry``.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

PLUGIN_PROTOCOL_VERSION = 1
PLUGIN_KINDS = frozenset({"agent", "match"})


@dataclass(frozen=True)
class RunnerContext:
    """Stable context passed to an agent or game runner plugin."""

    execution_id: str
    task_ref: str
    evaluation_kind: str
    task_path: str
    submission_path: str
    participants: Mapping[str, str] = field(default_factory=dict)
    seats: tuple[Mapping[str, Any], ...] = ()
    seed: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PLUGIN_PROTOCOL_VERSION,
            "execution_id": self.execution_id,
            "task_ref": self.task_ref,
            "evaluation_kind": self.evaluation_kind,
            "task_path": self.task_path,
            "submission_path": self.submission_path,
            "participants": dict(self.participants),
            "seats": [dict(seat) for seat in self.seats],
            "seed": self.seed,
            "metadata": dict(self.metadata),
        }


class RunnerPlugin(Protocol):
    """Plugin interface implemented by evaluator image authors."""

    name: str
    version: str
    kinds: frozenset[str]

    def run(self, context: RunnerContext) -> Mapping[str, Any]: ...


def _failed(reason: str) -> dict[str, Any]:
    return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": reason[:2000]}


def normalize_plugin_result(raw: Any) -> dict[str, Any]:
    """Normalize and validate the terminal result returned by a plugin."""

    if not isinstance(raw, Mapping):
        return _failed(f"runner returned unsupported type {type(raw).__name__}")
    status = raw.get("status")
    if status not in {"completed", "failed"}:
        return _failed("runner result status must be completed or failed")
    score = raw.get("score", 0.0 if status == "failed" else None)
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        return _failed("runner result score must be a finite number")
    metrics = raw.get("metrics", {})
    if not isinstance(metrics, Mapping):
        return _failed("runner result metrics must be an object")
    result = {"status": status, "score": float(score), "metrics": dict(metrics)}
    if raw.get("failure_reason") is not None:
        result["failure_reason"] = str(raw["failure_reason"])[:2000]
    for key in ("scores", "replay"):
        if key in raw:
            result["metrics"][key] = raw[key]
    if status == "failed" and not result.get("failure_reason"):
        result["failure_reason"] = "runner failed without a reason"
    return result


class PythonTaskPlugin:
    """Reference plugin that loads ``runner.py`` from the task package."""

    name = "python-task-runner"
    version = "1"
    kinds = frozenset({"agent", "match"})

    def run(self, context: RunnerContext) -> Mapping[str, Any]:
        runner_name = "runner.py"
        manifest = Path(context.task_path) / "judge.yaml"
        if manifest.is_file():
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("entrypoint:"):
                    runner_name = line.split(":", 1)[1].strip().strip("\"'")
                    break
        runner_path = Path(context.task_path) / runner_name
        if not runner_path.is_file():
            return _failed("plugin task is missing runner.py")
        spec = importlib.util.spec_from_file_location("brunost_task_runner", runner_path)
        if spec is None or spec.loader is None:
            return _failed("could not load task runner.py")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            entrypoint = getattr(module, "run", None)
            if not callable(entrypoint):
                return _failed("runner.py must define run(context)")
            return normalize_plugin_result(entrypoint(context.as_dict()))
        except Exception as exc:  # noqa: BLE001 - plugin failures become results
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            return _failed(f"runner plugin failed: {detail}")


class RunnerRegistry:
    """Registry for trusted evaluator plugins installed in an image."""

    def __init__(self) -> None:
        self._plugins: dict[str, RunnerPlugin] = {}

    def register(self, plugin: RunnerPlugin, *, replace: bool = False) -> None:
        if not plugin.name or not plugin.version:
            raise ValueError("runner plugins need name and version")
        if not plugin.kinds or not set(plugin.kinds).issubset(PLUGIN_KINDS):
            raise ValueError(f"runner plugin kinds must be a subset of {sorted(PLUGIN_KINDS)}")
        for kind in plugin.kinds:
            if kind in self._plugins and not replace:
                raise ValueError(f"runner plugin already registered for {kind}")
            self._plugins[kind] = plugin

    def get(self, kind: str) -> RunnerPlugin:
        try:
            return self._plugins[kind]
        except KeyError as exc:
            raise LookupError(f"no runner plugin registered for {kind}") from exc

    def run(self, kind: str, context: RunnerContext) -> dict[str, Any]:
        return normalize_plugin_result(self.get(kind).run(context))

    def names(self) -> dict[str, str]:
        return {kind: plugin.name for kind, plugin in sorted(self._plugins.items())}


def default_registry() -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register(PythonTaskPlugin())
    module_name = os.environ.get("BRUNOST_JUDGE_RUNNER_PLUGIN_MODULE", "").strip()
    if module_name:
        module = __import__(module_name, fromlist=["register"])
        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(f"runner plugin module {module_name!r} must define register(registry)")
        register(registry)
    return registry


def read_context_manifest(submission_path: str) -> dict[str, Any]:
    """Read the worker-created plugin manifest from a staged submission."""

    path = Path(submission_path) / ".brunost" / "plugin.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
