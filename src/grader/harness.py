"""Sandbox harness for generic scorer and v2 model task execution.

Pure standard-library (task evaluators may use numpy/pandas; the harness itself
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


def _run_model_evaluator_process(
    evaluator_path: str,
    predictions_path: str,
    labels_path: str,
    *,
    timeout_ms: int,
    memory_mb: int,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the strict v2 evaluator with only the selected split files."""

    if timeout_ms <= 0:
        return _failed("evaluator exceeded its time limit", metrics={"verdict": "time_limit_exceeded"})
    command = [sys.executable, "-m", "grader.model_evaluator_process", evaluator_path, predictions_path, labels_path]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(Path(evaluator_path).parent),
            env=_child_environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_submission_preexec(memory_mb, timeout_ms, drop_privileges=True),  # noqa: PLW1509
        )
    except OSError as exc:
        return _failed(f"could not start evaluator process: {exc}")
    started = time.monotonic()
    try:
        stdout, _ = process.communicate(timeout=max(0.001, timeout_ms / 1000))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait()
        return _failed("evaluator exceeded its time limit", metrics={"verdict": "time_limit_exceeded"})
    if process.returncode != 0:
        return _failed("evaluator process exited unsuccessfully", metrics={"verdict": "evaluator_error"})
    if len(stdout or b"") > _MAX_SCORER_OUTPUT_BYTES:
        return _failed("evaluator result exceeds the output limit")
    try:
        result = json.loads((stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed(f"evaluator returned invalid JSON: {exc}")
    if not isinstance(result, dict):
        return _failed("evaluator returned a non-object result")
    result.setdefault("metrics", {})
    result["metrics"].setdefault("evaluator_time_ms", int((time.monotonic() - started) * 1000))
    return result


def _run_model_submission_v2(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Run the strict v2 train/model/predict lifecycle.

    Training and prediction are separate processes and separate workspaces.
    This makes the model artifact explicit while ensuring training cannot read
    the private test set or labels.
    """

    profile = os.environ.get("BRUNOST_EVALUATION_PROFILE", "live").strip().lower() or "live"
    if profile in {"post", "generalization"}:
        profile = "post_competition"
    if profile not in {"live", "post_competition"}:
        return _failed("unknown model evaluation profile")
    if profile == "post_competition" and (_manifest_field(assets_path, "post_competition_enabled") or "false").lower() not in {"1", "true", "yes", "on"}:
        return _failed("post-competition evaluation is not enabled for this task")

    submission_root = Path(submission_path).resolve()
    entrypoint_name = _manifest_field(assets_path, "submission_entrypoint") or "submission.py"
    evaluator_name = _manifest_field(assets_path, "post_evaluator_entrypoint") if profile == "post_competition" else "evaluator.py"
    evaluator_name = evaluator_name or "evaluator.py"
    if profile == "post_competition":
        train_name = _manifest_field(assets_path, "post_training_dataset")
        test_name = _manifest_field(assets_path, "post_test_dataset")
        labels_name = _manifest_field(assets_path, "post_labels_dataset")
        training_time_field = "post_training_time_limit_ms"
        prediction_time_field = "post_prediction_time_limit_ms"
        evaluator_time_field = "post_evaluator_time_limit_ms"
    else:
        train_name = _manifest_field(assets_path, "training_dataset")
        test_name = _manifest_field(assets_path, "private_test_dataset")
        labels_name = _manifest_field(assets_path, "private_labels_dataset")
        training_time_field = "training_time_limit_ms"
        prediction_time_field = "prediction_time_limit_ms"
        evaluator_time_field = "evaluator_time_limit_ms"
    if not train_name or not test_name or not labels_name:
        return _failed("model task is missing its training, test, or labels dataset")
    try:
        entrypoint = _submission_file(submission_root, entrypoint_name, "submission entrypoint")
        if not entrypoint.is_file():
            return _failed(f"submission is missing {entrypoint_name}")
        training_source = _task_file(assets_path, train_name, "training dataset")
        private_test_source = _task_file(assets_path, test_name, "test dataset")
        labels_source = _task_file(assets_path, labels_name, "labels dataset")
        evaluator_source = _task_file(assets_path, evaluator_name, "evaluator")
        training_time_ms = int(_manifest_field(assets_path, training_time_field) or "120000")
        prediction_time_ms = int(_manifest_field(assets_path, prediction_time_field) or "10000")
        evaluator_time_ms = int(_manifest_field(assets_path, evaluator_time_field) or "10000")
        memory_mb = int(_manifest_field(assets_path, "memory_limit_mb") or str(_DEFAULT_MODEL_MEMORY_MB))
        model_max_bytes = int(_manifest_field(assets_path, "model_max_bytes") or str(_MAX_PREDICTION_MAX_BYTES))
    except (FileNotFoundError, ValueError) as exc:
        return _failed(str(exc))
    if profile == "live":
        public_test_name = _manifest_field(assets_path, "public_test_dataset")
        public_labels_name = _manifest_field(assets_path, "public_labels_dataset")
        if bool(public_test_name) != bool(public_labels_name):
            return _failed("public test and public labels must be declared together")
        public_test_source = _task_file(assets_path, public_test_name, "public test dataset") if public_test_name else None
        public_labels_source = _task_file(assets_path, public_labels_name, "public labels dataset") if public_labels_name else None
    else:
        public_test_source = public_labels_source = None
    if model_max_bytes < 1_024 or model_max_bytes > _MAX_PREDICTION_MAX_BYTES:
        return _failed("model task has an invalid model artifact limit")

    baseline_enabled = (_manifest_field(assets_path, "baseline_enabled") or "false").lower() in {"1", "true", "yes", "on"}
    baseline_source: Path | None = None
    if baseline_enabled:
        try:
            baseline_source = _task_file(assets_path, _manifest_field(assets_path, "baseline_entrypoint") or "private/baseline.py", "baseline")
        except (FileNotFoundError, ValueError) as exc:
            return _failed(str(exc))
    splits: list[tuple[str, Path, Path]] = []
    if public_test_source is not None and public_labels_source is not None:
        splits.append(("public", public_test_source, public_labels_source))
    splits.append(("private", private_test_source, labels_source))
    solution_count = 2 if baseline_enabled else 1
    calculated_budget_ms = solution_count * (training_time_ms + len(splits) * prediction_time_ms + len(splits) * evaluator_time_ms) + 5_000
    try:
        declared_budget_ms = int(_manifest_field(assets_path, "time_limit_ms") or str(calculated_budget_ms))
    except ValueError:
        return _failed("model task has an invalid total time limit")
    budget_ms = min(calculated_budget_ms, declared_budget_ms)
    if budget_ms < 1 or training_time_ms < 1 or prediction_time_ms < 1 or evaluator_time_ms < 1:
        return _failed("model task has invalid phase time limits")

    with tempfile.TemporaryDirectory(prefix="brunost-model-v2-") as temporary:
        root = Path(temporary)
        # Contestant code runs as the dedicated unprivileged UID.  Keep the
        # temporary workspace searchable while individual files stay private.
        root.chmod(0o755)
        deadline = time.monotonic() + budget_ms / 1000

        def phase_timeout(limit_ms: int, label: str) -> tuple[int | None, dict[str, Any] | None]:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining <= 0:
                return None, _failed(f"{label} exceeded the model evaluation time budget", metrics={"verdict": "time_limit_exceeded"})
            return min(limit_ms, remaining), None

        def run_solution(label: str, source: Path) -> tuple[dict[str, Path] | None, dict[str, Any] | None]:
            training_workspace = root / f"{label}-training"
            training_workspace.mkdir(parents=True)
            training_workspace.chmod(0o755)
            train_input = training_workspace / "input" / "training" / training_source.name
            train_input.parent.mkdir(parents=True)
            train_input.parent.parent.chmod(0o755)
            train_input.parent.chmod(0o755)
            shutil.copyfile(training_source, train_input)
            train_input.chmod(0o644)
            submission_workspace = training_workspace / "submission"
            if label == "participant":
                shutil.copytree(submission_root, submission_workspace, symlinks=False)
            else:
                submission_workspace.mkdir()
            code_path = _submission_file(submission_workspace, entrypoint_name, "submission entrypoint")
            if label != "participant":
                # Baselines are task-owned files; keep the same entrypoint shape
                # as participant submissions so both use the identical contract.
                code_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, code_path)
            code_path.chmod(0o644)
            model_path = training_workspace / "model" / "model.bin"
            model_path.parent.mkdir()
            model_path.parent.chmod(0o733)
            timeout_ms, failure = phase_timeout(training_time_ms, f"{label} training")
            if failure is not None:
                return None, failure
            training_env = _child_environment({
                "BRUNOST_ML_PHASE": "train",
                "BRUNOST_ML_TRAINING_DATASET": str(train_input),
                "BRUNOST_ML_MODEL_PATH": str(model_path),
                "BRUNOST_ML_SEED": _manifest_field(assets_path, "seed") or "42",
                "PYTHONHASHSEED": _manifest_field(assets_path, "seed") or "42",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            result = _run_submission_process(
                [sys.executable, "-m", "grader.model_process", str(code_path), "train", str(train_input), str(model_path)],
                training_workspace,
                training_env,
                timeout_ms=timeout_ms or 1,
                memory_mb=memory_mb,
                output_limit_bytes=model_max_bytes,
            )
            if result.get("status") != "completed":
                return None, result
            if not model_path.is_file() or model_path.stat().st_size == 0:
                return None, _failed(f"{label} training did not create model.bin")
            if model_path.stat().st_size > model_max_bytes:
                return None, _failed(f"{label} model artifact exceeds the model size limit")

            outputs: dict[str, Path] = {}
            for split, test_source, _labels in splits:
                prediction_workspace = root / f"{label}-{split}-prediction"
                prediction_workspace.mkdir(parents=True)
                prediction_workspace.chmod(0o755)
                test_input = prediction_workspace / "input" / "test" / test_source.name
                test_input.parent.mkdir(parents=True)
                test_input.parent.parent.chmod(0o755)
                test_input.parent.chmod(0o755)
                shutil.copyfile(test_source, test_input)
                test_input.chmod(0o644)
                prediction_model = prediction_workspace / "model.bin"
                shutil.copyfile(model_path, prediction_model)
                prediction_model.chmod(0o444)
                output_path = prediction_workspace / "output" / "predictions.csv"
                output_path.parent.mkdir()
                output_path.parent.chmod(0o733)
                timeout_ms, failure = phase_timeout(prediction_time_ms, f"{label} {split} prediction")
                if failure is not None:
                    return None, failure
                prediction_env = _child_environment({
                    "BRUNOST_ML_PHASE": "predict",
                    "BRUNOST_ML_MODEL_PATH": str(prediction_model),
                    "BRUNOST_ML_TEST_DATASET": str(test_input),
                    "BRUNOST_ML_OUTPUT_PATH": str(output_path),
                    "BRUNOST_ML_SEED": _manifest_field(assets_path, "seed") or "42",
                    "PYTHONHASHSEED": _manifest_field(assets_path, "seed") or "42",
                    "PYTHONDONTWRITEBYTECODE": "1",
                })
                result = _run_submission_process(
                    [sys.executable, "-m", "grader.model_process", str(code_path), "predict", str(prediction_model), str(test_input), str(output_path)],
                    prediction_workspace,
                    prediction_env,
                    timeout_ms=timeout_ms or 1,
                    memory_mb=memory_mb,
                    output_limit_bytes=_MAX_PREDICTION_MAX_BYTES,
                )
                if result.get("status") != "completed":
                    return None, result
                if not output_path.is_file() or output_path.stat().st_size == 0:
                    return None, _failed(f"{label} {split} prediction did not create a non-empty predictions.csv")
                if output_path.stat().st_size > _MAX_PREDICTION_MAX_BYTES:
                    return None, _failed(f"{label} {split} predictions exceed the output size limit")
                outputs[split] = output_path
            return outputs, None

        def evaluate_outputs(label: str, outputs: dict[str, Path]) -> tuple[dict[str, float] | None, dict[str, Any] | None]:
            scores: dict[str, float] = {}
            for split, predictions_path in outputs.items():
                labels_path = next(labels for split_name, _test, labels in splits if split_name == split)
                evaluator_workspace = root / f"{label}-{split}-evaluator"
                evaluator_workspace.mkdir(parents=True)
                evaluator_workspace.chmod(0o755)
                evaluator_path = evaluator_workspace / "evaluator.py"
                shutil.copyfile(evaluator_source, evaluator_path)
                evaluator_path.chmod(0o644)
                copied_predictions = evaluator_workspace / "predictions.csv"
                copied_labels = evaluator_workspace / "labels"
                shutil.copyfile(predictions_path, copied_predictions)
                shutil.copyfile(labels_path, copied_labels)
                copied_predictions.chmod(0o644)
                copied_labels.chmod(0o644)
                timeout_ms, failure = phase_timeout(evaluator_time_ms, f"{label} {split} evaluator")
                if failure is not None:
                    return None, failure
                result = _run_model_evaluator_process(
                    str(evaluator_path),
                    str(copied_predictions),
                    str(copied_labels),
                    timeout_ms=timeout_ms or 1,
                    memory_mb=memory_mb,
                    environment=_child_environment({
                        "BRUNOST_ML_PROFILE": profile,
                        "BRUNOST_ML_SPLIT": split,
                        "BRUNOST_ML_PREDICTIONS_PATH": str(copied_predictions),
                        "BRUNOST_ML_LABELS_PATH": str(copied_labels),
                    }),
                )
                if result.get("status") != "completed":
                    return None, result
                try:
                    scores[split] = float(result["score"])
                except (KeyError, TypeError, ValueError):
                    return None, _failed(f"{label} {split} evaluator returned no numeric score")
            return scores, None

        metrics: dict[str, Any] = {"profile": profile}
        if baseline_source is not None:
            baseline_outputs, failure = run_solution("baseline", baseline_source)
            if failure is not None or baseline_outputs is None:
                return failure or _failed("baseline evaluation failed")
            baseline_scores, failure = evaluate_outputs("baseline", baseline_outputs)
            if failure is not None or baseline_scores is None:
                return failure or _failed("baseline evaluation failed")
            metrics.update({f"baseline_{key}": value for key, value in baseline_scores.items()})

        participant_outputs, failure = run_solution("participant", entrypoint)
        if failure is not None or participant_outputs is None:
            return failure or _failed("participant evaluation failed")
        participant_scores, failure = evaluate_outputs("participant", participant_outputs)
        if failure is not None or participant_scores is None:
            return failure or _failed("participant evaluation failed")
        metrics.update(participant_scores)
        official = participant_scores.get("private")
        if official is None:
            return _failed("private evaluation did not produce an official score", metrics=metrics)
        return {"status": "completed", "score": official, "metrics": metrics}


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
        if kind == "optimization":
            from grader.optimization import run_optimization

            return run_optimization(submission_path, assets_path)
        if kind == "model":
            return _run_model_submission_v2(submission_path, assets_path)
        return _run_scorer(submission_path, assets_path)
    except Exception as exc:  # noqa: BLE001 — the harness must contain all scorer errors
        detail = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=3)
        return _failed(f"Scorer raised an exception — {detail}\n{tb}")
