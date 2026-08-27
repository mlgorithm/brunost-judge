"""Scorer harness: run a task's metrics.py and normalize its output.

Pure standard-library (the task's metrics.py may use numpy/pandas; the harness itself
does not). No Brunost backend imports — this is the extraction seam (ADR-0010).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

_MAX_REASON_CHARS = 2000
_MAX_SCORER_OUTPUT_BYTES = 1_000_000
_DEFAULT_MODEL_TIME_MS = 120_000
_DEFAULT_MODEL_MEMORY_MB = 2_048
_DEFAULT_PREDICTION_MAX_BYTES = 10_000_000
_MAX_PREDICTION_MAX_BYTES = 64_000_000
_CHILD_ENV_KEYS = (
    "PATH",
    "PYTHONPATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
)


def _finite_float(value: Any) -> float | None:
    """Coerce to a finite float, or None if it isn't a real number."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normalize_result(
    raw: Any,
    *,
    official_split: str | None = None,
    require_official: bool = False,
) -> dict[str, Any]:
    """Map whatever a task scorer returned into the canonical result contract.

    Returns {"status", "score", "metrics"[, "failure_reason"]}. Legacy scorers
    default to their public score. Model tasks explicitly select their official
    split, which is private for contest rankings.
    Accepts: a number; {"public","private","*_detail"}; or the IOAI
    {"score": {"public_a","private_b","*_detail"}} shape.
    """
    metrics: dict[str, Any] = {}
    public: float | None = None
    private: float | None = None

    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return _failed("Scorer returned a boolean, not a score")
    if isinstance(raw, (int, float)):
        if official_split == "private":
            private = _finite_float(raw)
        else:
            public = _finite_float(raw)
    elif isinstance(raw, dict):
        if isinstance(raw.get("metrics"), dict):
            metrics.update(raw["metrics"])
        score_field = raw.get("score")
        if isinstance(score_field, dict):  # IOAI nested shape
            public = _finite_float(score_field.get("public_a", score_field.get("public")))
            private = _finite_float(score_field.get("private_b", score_field.get("private")))
            for key in ("public_detail", "private_detail"):
                if isinstance(score_field.get(key), dict):
                    metrics[key] = score_field[key]
        elif score_field is not None:
            if official_split == "private":
                private = _finite_float(score_field)
            else:
                public = _finite_float(score_field)
        # flat top-level keys win if present
        if raw.get("public") is not None:
            public = _finite_float(raw["public"])
        if raw.get("private") is not None:
            private = _finite_float(raw["private"])
        for key in ("public_detail", "private_detail"):
            if isinstance(raw.get(key), dict):
                metrics[key] = raw[key]
    else:
        return _failed(f"Scorer returned unsupported type {type(raw).__name__}")

    if public is not None:
        metrics["public"] = public
    if private is not None:
        metrics["private"] = private

    if require_official and official_split == "private" and private is None:
        return _failed("Scorer did not return the required private score", metrics=metrics)
    score = private if official_split == "private" else (public if public is not None else private)
    if score is None:
        return _failed("Scorer returned no usable numeric score", metrics=metrics)
    return {"status": "completed", "score": score, "metrics": metrics}


def _failed(reason: str, *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "failed",
        "score": 0.0,
        "metrics": metrics or {},
        "failure_reason": reason[:_MAX_REASON_CHARS],
    }


def _load_metrics_module(assets_path: str):
    """Import metrics.py from either the legacy or packaged task layout."""
    candidates = (
        os.path.join(assets_path, "metrics.py"),
        os.path.join(assets_path, "scorer", "metrics.py"),
    )
    metrics_file = next((path for path in candidates if os.path.isfile(path)), None)
    if metrics_file is None:
        return None
    spec = importlib.util.spec_from_file_location("task_metrics", metrics_file)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Permit a packaged scorer to import helper modules beside metrics.py while
    # keeping the import scoped to this task invocation.
    search_paths = [os.path.dirname(metrics_file), assets_path]
    original_path = list(__import__("sys").path)
    __import__("sys").path[:0] = search_paths
    try:
        spec.loader.exec_module(module)
    finally:
        __import__("sys").path[:] = original_path
    return module


def _task_kind(assets_path: str) -> str:
    manifest_path = os.path.join(assets_path, "judge.yaml")
    if not os.path.isfile(manifest_path):
        return ""
    with open(manifest_path, encoding="utf-8") as manifest_file:
        return next(
            (
                line.split(":", 1)[1].strip().strip("\"'").lower()
                for line in manifest_file
                if line.strip().startswith("kind:")
            ),
            "",
        )


def _manifest_field(assets_path: str, name: str) -> str | None:
    manifest_path = Path(assets_path) / "judge.yaml"
    if not manifest_path.is_file():
        return None
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == name.lower():
            return value.strip().strip("\"'")
    return None


def _task_file(assets_path: str, relative: str, label: str) -> Path:
    root = Path(assets_path).resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{label} escapes the task bundle")
    if not path.is_file():
        raise FileNotFoundError(f"model task is missing {label}: {relative}")
    return path


def _submission_file(root: Path, relative: str, label: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"{label} must stay inside the submission bundle")
    return path


def _child_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Build a deliberately small environment for task-controlled code."""

    environment = {key: os.environ[key] for key in _CHILD_ENV_KEYS if os.environ.get(key) is not None}
    if "PYTHONPATH" in environment:
        environment["PYTHONPATH"] = os.pathsep.join(
            str(Path(part).resolve())
            for part in environment["PYTHONPATH"].split(os.pathsep)
            if part
        )
    environment.update(overrides or {})
    return environment


def _submission_preexec(
    memory_mb: int,
    timeout_ms: int,
    *,
    output_limit_bytes: int | None = None,
    drop_privileges: bool = True,
):
    """Apply per-submission resource limits before Python starts."""

    if os.name != "posix":
        return None

    def limit() -> None:
        try:
            import resource

            memory_bytes = max(64, int(memory_mb)) * 1024 * 1024
            if hasattr(resource, "RLIMIT_AS"):
                _, current_hard = resource.getrlimit(resource.RLIMIT_AS)
                hard = memory_bytes if current_hard == resource.RLIM_INFINITY else min(current_hard, memory_bytes)
                resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
            if hasattr(resource, "RLIMIT_CPU"):
                cpu_seconds = max(1, (int(timeout_ms) + 999) // 1000 + 1)
                _, current_hard = resource.getrlimit(resource.RLIMIT_CPU)
                hard = cpu_seconds if current_hard == resource.RLIM_INFINITY else min(current_hard, cpu_seconds)
                resource.setrlimit(resource.RLIMIT_CPU, (hard, hard))
            if output_limit_bytes is not None and hasattr(resource, "RLIMIT_FSIZE"):
                output_bytes = max(1, int(output_limit_bytes))
                _, current_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
                hard = output_bytes if current_hard == resource.RLIM_INFINITY else min(current_hard, output_bytes)
                resource.setrlimit(resource.RLIMIT_FSIZE, (hard, hard))
        except (ImportError, OSError, ValueError):
            # The outer Judge sandbox still enforces its own limits. Some local
            # development platforms do not permit every rlimit operation.
            pass
        if drop_privileges and os.geteuid() == 0:
            # The evaluator needs root to read the root-only task bundle,
            # but the participant must not inherit that privilege.
            os.setgroups([])
            os.setgid(65533)
            os.setuid(65533)

    return limit


def _run_submission_process(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    timeout_ms: int,
    memory_mb: int,
    output_limit_bytes: int | None = None,
    drop_privileges: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_submission_preexec(  # noqa: PLW1509
                memory_mb,
                timeout_ms,
                output_limit_bytes=output_limit_bytes,
                drop_privileges=drop_privileges,
            ),
        )
    except OSError as exc:
        return _failed(f"could not start training process: {exc}")
    try:
        process.communicate(timeout=max(0.001, timeout_ms / 1000))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait()
        return _failed(
            f"training process exceeded its {timeout_ms} ms limit",
            metrics={"verdict": "time_limit_exceeded", "training_time_ms": int((time.monotonic() - started) * 1000)},
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if process.returncode != 0:
        return _failed("training process exited unsuccessfully", metrics={"verdict": "runtime_error", "training_time_ms": elapsed_ms})
    return {"status": "completed", "score": None, "metrics": {"training_time_ms": elapsed_ms}}


def _run_scorer(
    submission_path: str,
    assets_path: str,
    *,
    official_split: str | None = None,
    require_official: bool = False,
) -> dict[str, Any]:
    module = _load_metrics_module(assets_path)
    if module is None:
        return _failed("Task has no metrics.py scorer in its assets")
    evaluate = getattr(module, "evaluate", None)
    if not callable(evaluate):
        return _failed("Task metrics.py must define evaluate(submission_path, assets_path)")
    raw = evaluate(submission_path, assets_path)
    return normalize_result(raw, official_split=official_split, require_official=require_official)


def _run_scorer_process(
    submission_path: str,
    assets_path: str,
    *,
    timeout_ms: int,
    memory_mb: int,
    official_split: str | None = None,
    require_official: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run task-controlled scoring code outside the evaluator process."""

    if timeout_ms <= 0:
        return _failed("scorer exceeded the model evaluation time budget", metrics={"verdict": "time_limit_exceeded"})
    command = [sys.executable, "-m", "grader.scorer_process", submission_path, assets_path]
    if official_split:
        command.extend(["--official-split", official_split])
    if require_official:
        command.append("--require-official")
    try:
        process = subprocess.Popen(
            command,
            cwd=assets_path,
            env=_child_environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_submission_preexec(memory_mb, timeout_ms, drop_privileges=False),  # noqa: PLW1509
        )
    except OSError as exc:
        return _failed(f"could not start scorer process: {exc}")
    started = time.monotonic()
    try:
        stdout, _ = process.communicate(timeout=max(0.001, timeout_ms / 1000))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait()
        return _failed("scorer exceeded the model evaluation time budget", metrics={"verdict": "time_limit_exceeded"})
    if process.returncode != 0:
        return _failed("scorer process exited unsuccessfully", metrics={"verdict": "scorer_error"})
    if len(stdout or b"") > _MAX_SCORER_OUTPUT_BYTES:
        return _failed("scorer result exceeds the output limit")
    try:
        result = json.loads((stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed(f"scorer returned invalid JSON: {exc}")
    if not isinstance(result, dict):
        return _failed("scorer returned a non-object result")
    result.setdefault("metrics", {})
    result["metrics"].setdefault("scoring_time_ms", int((time.monotonic() - started) * 1000))
    return result


def _run_model_submission(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Run a Python train/predict submission without exposing private labels."""

    submission_root = Path(submission_path).resolve()
    entrypoint = _manifest_field(assets_path, "submission_entrypoint") or "submission.py"
    prediction_output = _manifest_field(assets_path, "prediction_output") or "predictions.csv"
    public_dataset = _manifest_field(assets_path, "public_dataset")
    hidden_dataset = _manifest_field(assets_path, "hidden_dataset")
    hidden_labels = _manifest_field(assets_path, "hidden_labels_dataset")
    if not public_dataset or not hidden_dataset or not hidden_labels:
        return _failed("python_code model tasks need public, hidden, and hidden-label datasets")
    try:
        entrypoint_path = _submission_file(submission_root, entrypoint, "submission entrypoint")
        if not entrypoint_path.is_file():
            return _failed(f"submission is missing {entrypoint}")
        public_path = _task_file(assets_path, public_dataset, "public dataset")
        hidden_path = _task_file(assets_path, hidden_dataset, "hidden dataset")
        labels_path = _task_file(assets_path, hidden_labels, "hidden labels dataset")
        total_time_ms = int(_manifest_field(assets_path, "time_limit_ms") or str(_DEFAULT_MODEL_TIME_MS))
        training_time_ms = int(_manifest_field(assets_path, "training_time_limit_ms") or str(total_time_ms))
        memory_mb = int(_manifest_field(assets_path, "memory_limit_mb") or str(_DEFAULT_MODEL_MEMORY_MB))
        prediction_limit_bytes = int(
            _manifest_field(assets_path, "prediction_max_bytes") or str(_DEFAULT_PREDICTION_MAX_BYTES)
        )
    except (FileNotFoundError, ValueError) as exc:
        return _failed(str(exc))
    if total_time_ms < 1 or training_time_ms < 1 or training_time_ms > total_time_ms:
        return _failed("model task has invalid time limits")
    if not 1_024 <= prediction_limit_bytes <= _MAX_PREDICTION_MAX_BYTES:
        return _failed("model task has an invalid prediction output limit")

    with tempfile.TemporaryDirectory(prefix="brunost-model-run-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        public_input = root / "input" / "public" / public_path.name
        hidden_input = root / "input" / "private" / hidden_path.name
        public_input.parent.mkdir(parents=True)
        hidden_input.parent.mkdir(parents=True)
        public_input.parent.chmod(0o755)
        hidden_input.parent.chmod(0o755)
        shutil.copyfile(public_path, public_input)
        shutil.copyfile(hidden_path, hidden_input)
        public_input.chmod(0o644)
        hidden_input.chmod(0o644)
        output_path = _submission_file(root / "output", prediction_output, "prediction output")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        (root / "output").chmod(0o733)
        output_path.parent.chmod(0o733)

        base_env = _child_environment({
            "BRUNOST_ML_PUBLIC_DATASET": str(public_input),
            "BRUNOST_ML_PRIVATE_DATASET": str(hidden_input),
            "BRUNOST_ML_OUTPUT_PATH": str(output_path),
            "BRUNOST_ML_SEED": _manifest_field(assets_path, "seed") or "42",
            "PYTHONHASHSEED": _manifest_field(assets_path, "seed") or "42",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        deadline = time.monotonic() + total_time_ms / 1000

        def remaining_ms() -> int:
            return max(0, int((deadline - time.monotonic()) * 1000))

        def phase_timeout(limit_ms: int, label: str) -> tuple[int | None, dict[str, Any] | None]:
            remaining = remaining_ms()
            if remaining <= 0:
                return None, _failed(
                    f"{label} exceeded the model evaluation time budget",
                    metrics={"verdict": "time_limit_exceeded"},
                )
            return min(limit_ms, remaining), None

        baseline_enabled = (_manifest_field(assets_path, "baseline_enabled") or "false").lower() in {"1", "true", "yes", "on"}
        baseline_output: Path | None = None
        if baseline_enabled:
            baseline_entrypoint = _manifest_field(assets_path, "baseline_entrypoint") or "private/baseline.py"
            try:
                baseline_path = _task_file(assets_path, baseline_entrypoint, "baseline solution")
            except (FileNotFoundError, ValueError) as exc:
                return _failed(str(exc))
            baseline_output = _submission_file(root / "baseline", prediction_output, "prediction output")
            baseline_output.parent.mkdir(parents=True, exist_ok=True)
            baseline_output.parent.chmod(0o700)
            baseline_env = {**base_env, "BRUNOST_ML_OUTPUT_PATH": str(baseline_output), "BRUNOST_ML_BASELINE": "1"}
            timeout_ms, failure = phase_timeout(training_time_ms, "baseline")
            if failure is not None:
                return failure
            baseline_result = _run_submission_process(
                [sys.executable, str(baseline_path)],
                baseline_path.parent,
                baseline_env,
                timeout_ms=timeout_ms or 1,
                memory_mb=memory_mb,
                output_limit_bytes=prediction_limit_bytes,
                drop_privileges=False,
            )
            if baseline_result.get("status") != "completed" or not baseline_output.is_file():
                return _failed("baseline solution did not produce a valid prediction file")
            if baseline_output.stat().st_size == 0 or baseline_output.stat().st_size > prediction_limit_bytes:
                return _failed("baseline prediction file is empty or exceeds the output limit")

        participant_env = {**base_env, "BRUNOST_ML_BASELINE": "0"}
        timeout_ms, failure = phase_timeout(training_time_ms, "participant")
        if failure is not None:
            return failure
        participant_result = _run_submission_process(
            [sys.executable, str(entrypoint_path)],
            submission_root,
            participant_env,
            timeout_ms=timeout_ms or 1,
            memory_mb=memory_mb,
            output_limit_bytes=prediction_limit_bytes,
        )
        if participant_result.get("status") != "completed":
            return participant_result
        if not output_path.is_file():
            return _failed(f"submission did not create {prediction_output}")
        if output_path.stat().st_size == 0 or output_path.stat().st_size > prediction_limit_bytes:
            return _failed("prediction output is empty or exceeds the output limit")

        scored_submission = root / "scored-submission"
        shutil.copytree(submission_root, scored_submission)
        scored_prediction = _submission_file(scored_submission, prediction_output, "prediction output")
        scored_prediction.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output_path, scored_prediction)
        scorer_values = {
            "BRUNOST_ML_PREDICTIONS_PATH": str(scored_prediction),
            "BRUNOST_ML_PRIVATE_LABELS": str(labels_path),
            "BRUNOST_ML_PUBLIC_DATASET": str(public_path),
            "BRUNOST_ML_PRIVATE_DATASET": str(hidden_path),
        }
        if baseline_output is not None:
            scorer_values["BRUNOST_ML_BASELINE_PREDICTIONS_PATH"] = str(baseline_output)
        timeout_ms, failure = phase_timeout(total_time_ms, "scorer")
        if failure is not None:
            return failure
        result = _run_scorer_process(
            str(scored_submission),
            assets_path,
            timeout_ms=timeout_ms or 1,
            memory_mb=memory_mb,
            official_split="private",
            require_official=True,
            environment=_child_environment(scorer_values),
        )
        result.setdefault("metrics", {})
        if participant_result.get("metrics", {}).get("training_time_ms") is not None:
            result["metrics"]["training_time_ms"] = participant_result["metrics"]["training_time_ms"]
        return result


def _run_plugin(submission_path: str, assets_path: str) -> dict[str, Any]:
    from grader.plugins import RunnerContext, default_registry, read_context_manifest

    manifest = read_context_manifest(submission_path)
    kind = "match" if manifest.get("evaluation_kind") == "match" else "agent"
    participants = manifest.get("participants", {})
    if not isinstance(participants, dict):
        participants = {}
    root = Path(submission_path)
    resolved_participants = {
        str(agent_id): str((root / str(relative)).resolve())
        for agent_id, relative in participants.items()
        if isinstance(agent_id, str) and isinstance(relative, str)
    }
    seats = []
    for item in manifest.get("seats", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        seat = dict(item)
        seat["path"] = str((root / seat["path"]).resolve())
        seats.append(seat)
    context = RunnerContext(
        execution_id=str(manifest.get("execution_id") or "local-plugin-execution"),
        task_ref=str(manifest.get("task_ref") or "local-plugin-task"),
        evaluation_kind=kind,
        task_path=str(Path(assets_path).resolve()),
        submission_path=str(root.resolve()),
        participants=resolved_participants,
        seats=tuple(seats),
        seed=manifest.get("seed") if isinstance(manifest.get("seed"), int) else None,
        output_path=os.environ.get("RESULT_ARTIFACTS_PATH", "/tmp/brunost-output/artifacts"),
        metadata=manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {},
    )
    return default_registry().run(kind, context)


def run(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Score a submission with the task's metrics.py; never raise.

    Any failure (missing scorer, bad submission, scorer exception) returns a
    'failed' result with a reason instead of crashing the sandbox.
    """
    try:
        kind = _task_kind(assets_path)
        if kind in {"icpc", "interactive"}:
            from grader.classic import run_classic, run_interactive

            runner = run_interactive if kind == "interactive" else run_classic
            return runner(submission_path, assets_path)
        if kind in {"agent", "game"}:
            return _run_plugin(submission_path, assets_path)
        if kind == "model":
            submission_mode = (_manifest_field(assets_path, "submission_mode") or "scorer").lower()
            if submission_mode == "python_code":
                return _run_model_submission(submission_path, assets_path)
            # Legacy model packages predate the train/predict lifecycle and
            # keep the original public-score scorer behavior.
            timeout_ms = int(_manifest_field(assets_path, "time_limit_ms") or "15000")
            memory_mb = int(_manifest_field(assets_path, "memory_limit_mb") or "4096")
            return _run_scorer_process(
                submission_path,
                assets_path,
                timeout_ms=timeout_ms,
                memory_mb=memory_mb,
            )
        return _run_scorer(submission_path, assets_path)
    except Exception as exc:  # noqa: BLE001 — the harness must contain all scorer errors
        detail = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=3)
        return _failed(f"Scorer raised an exception — {detail}\n{tb}")
