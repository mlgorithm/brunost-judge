"""Feasibility-and-objective scorer for optimization task packages.

An optimization package contains ``tests/*.in`` instances, an evaluator with
``evaluate(input_path, output_path)``, and optionally a trusted baseline
program. Contestant programs never provide their own objective value: the
evaluator computes feasibility and objective from the candidate output.
"""

from __future__ import annotations

import contextlib
import importlib.util
import math
import multiprocessing
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grader.classic import (
    ClassicConfig,
    _compile,
    _CompileFailure,
    _copy_compiled_runtime,
    _limit_child,
    _manifest,
    _normalize_language,
    _run_process,
    _safe_relative,
    _source_path,
    _temporary_workspace,
)

MAX_DIAGNOSTIC_CHARS = 4000
DEFAULT_TIME_LIMIT_MS = 2000
DEFAULT_MEMORY_LIMIT_MB = 512
DEFAULT_OUTPUT_LIMIT_BYTES = 1 << 20
MIN_TIME_LIMIT_MS = 100
MAX_TIME_LIMIT_MS = 15_000
MIN_MEMORY_LIMIT_MB = 64
MAX_MEMORY_LIMIT_MB = 4_096
MIN_OUTPUT_LIMIT_BYTES = 1 << 10
MAX_OUTPUT_LIMIT_BYTES = 64 << 20


class OptimizationJudgeError(ValueError):
    """Raised when an optimization package or evaluator is invalid."""


class InvalidOptimizationOutput(ValueError):
    """Evaluator-raised verdict for a malformed or invalid candidate output.

    Task evaluators should either return ``feasible=False`` or raise this
    exception when the candidate output cannot be parsed.  Other evaluator
    exceptions remain task errors so broken author code is not silently
    converted into a contestant score.
    """


@dataclass(frozen=True)
class OptimizationConfig:
    language: str
    entrypoint: str | None
    evaluator_entrypoint: str
    baseline_entrypoint: str | None
    time_limit_ms: int
    memory_limit_mb: int
    output_limit_bytes: int
    objective_direction: str
    score_mode: str
    aggregation: str


def _failed(reason: str, *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "failed",
        "score": 0.0,
        "metrics": metrics or {"runner": "optimization", "schema_version": 1},
        "failure_reason": reason[:MAX_DIAGNOSTIC_CHARS],
    }


def _positive(value: str | None, default: int, label: str) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError as exc:
        raise OptimizationJudgeError(f"{label} must be an integer") from exc
    if parsed < 1:
        raise OptimizationJudgeError(f"{label} must be positive")
    return parsed


def _bounded(value: str | None, default: int, label: str, minimum: int, maximum: int) -> int:
    parsed = _positive(value, default, label)
    if not minimum <= parsed <= maximum:
        raise OptimizationJudgeError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _config(task: Path) -> OptimizationConfig:
    values = _manifest(task)
    if values.get("kind", "").lower() != "optimization":
        raise OptimizationJudgeError("optimization runner requires kind: optimization")
    if values.get("version") != "1":
        raise OptimizationJudgeError("optimization tasks must declare version: 1")
    if values.get("runner", "").lower() != "optimization":
        raise OptimizationJudgeError("optimization task must declare runner: optimization")
    language = _normalize_language(values.get("language", ""), "language")
    evaluator_entrypoint = values.get("evaluator_entrypoint") or "private/evaluator.py"
    direction = values.get("objective_direction", "").lower()
    score_mode = values.get("score_mode", "").lower()
    aggregation = values.get("aggregation", "").lower()
    if direction not in {"maximize", "minimize"}:
        raise OptimizationJudgeError("objective_direction must be maximize or minimize")
    if score_mode not in {"checker_score", "baseline_ratio"}:
        raise OptimizationJudgeError("score_mode must be checker_score or baseline_ratio")
    if aggregation not in {"mean", "minimum", "geometric_mean"}:
        raise OptimizationJudgeError("aggregation must be mean, minimum, or geometric_mean")
    baseline_enabled = values.get("baseline_enabled", "false").lower() in {"1", "true", "yes", "on"}
    baseline_entrypoint = (
        values.get("baseline_entrypoint") or "private/baseline.py" if baseline_enabled else None
    )
    if score_mode == "baseline_ratio" and not baseline_enabled:
        raise OptimizationJudgeError("baseline_ratio scoring requires an enabled baseline")
    return OptimizationConfig(
        language=language,
        entrypoint=values.get("entrypoint") or None,
        evaluator_entrypoint=evaluator_entrypoint,
        baseline_entrypoint=baseline_entrypoint,
        time_limit_ms=_bounded(
            values.get("time_limit_ms"), DEFAULT_TIME_LIMIT_MS, "time_limit_ms", MIN_TIME_LIMIT_MS, MAX_TIME_LIMIT_MS
        ),
        memory_limit_mb=_bounded(
            values.get("memory_limit_mb"),
            DEFAULT_MEMORY_LIMIT_MB,
            "memory_limit_mb",
            MIN_MEMORY_LIMIT_MB,
            MAX_MEMORY_LIMIT_MB,
        ),
        output_limit_bytes=_bounded(
            values.get("output_limit_bytes"),
            DEFAULT_OUTPUT_LIMIT_BYTES,
            "output_limit_bytes",
            MIN_OUTPUT_LIMIT_BYTES,
            MAX_OUTPUT_LIMIT_BYTES,
        ),
        objective_direction=direction,
        score_mode=score_mode,
        aggregation=aggregation,
    )


def _load_evaluator(task: Path, entrypoint: str):
    path = _safe_relative(task, entrypoint)
    if not path.is_file():
        raise OptimizationJudgeError(f"optimization evaluator does not exist: {entrypoint}")
    spec = importlib.util.spec_from_file_location("optimization_task_evaluator", path)
    if spec is None or spec.loader is None:
        raise OptimizationJudgeError("optimization evaluator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    sys.path.insert(0, str(task))
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise OptimizationJudgeError(f"optimization evaluator failed to load: {type(exc).__name__}: {exc}") from exc
    finally:
        sys.path[:] = original_path
    evaluate = getattr(module, "evaluate", None)
    if not callable(evaluate):
        raise OptimizationJudgeError("optimization evaluator must define evaluate(input_path, output_path)")
    return evaluate


def _inputs(task: Path) -> tuple[Path, ...]:
    root = task / "tests"
    inputs = tuple(sorted(root.rglob("*.in"))) if root.is_dir() else ()
    if not inputs:
        raise OptimizationJudgeError("optimization tasks need tests/*.in files")
    resolved_root = root.resolve()
    for input_path in inputs:
        resolved_input = input_path.resolve()
        if resolved_input != resolved_root and resolved_root not in resolved_input.parents:
            raise OptimizationJudgeError(f"optimization input escapes tests/: {input_path.name}")
        if not input_path.is_file():
            raise OptimizationJudgeError(f"optimization input is not a regular file: {input_path.name}")
    return inputs


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise OptimizationJudgeError(f"optimization {label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OptimizationJudgeError(f"optimization {label} must be numeric") from exc
    if not math.isfinite(parsed):
        raise OptimizationJudgeError(f"optimization {label} must be finite")
    return parsed


def _normalize_evaluation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OptimizationJudgeError("optimization evaluator must return an object")
    feasible = raw.get("feasible", raw.get("valid", raw.get("accepted", raw.get("ok"))))
    if not isinstance(feasible, bool):
        raise OptimizationJudgeError("optimization evaluator must return boolean feasible")
    objective = raw.get("objective")
    if feasible:
        if objective is None:
            raise OptimizationJudgeError("feasible optimization results need a numeric objective")
        objective = _finite_float(objective, "objective")
    elif objective is not None:
        objective = _finite_float(objective, "objective")
    score = raw.get("score")
    if score is not None:
        score = _finite_float(score, "score")
    return {
        "feasible": feasible,
        "objective": objective,
        "score": score,
        "message": str(raw.get("message") or "")[:1000],
    }


def _evaluation_worker(
    task_path: str,
    evaluator_entrypoint: str,
    input_path: str,
    output_path: str,
    connection: Any,
    memory_mb: int,
    timeout_ms: int,
) -> None:
    """Execute author code outside the runner process with bounded resources."""

    try:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
            "TMPDIR": str(Path(output_path).parent),
            "LANG": "C",
            **({"PYTHONPATH": os.environ["PYTHONPATH"]} if os.environ.get("PYTHONPATH") else {}),
        }
        os.environ.clear()
        os.environ.update(environment)
        _limit_child(memory_mb, timeout_ms, None)
        with (
            open(os.devnull, "w", encoding="utf-8") as devnull,
            contextlib.redirect_stdout(devnull),
            contextlib.redirect_stderr(devnull),
        ):
            evaluate = _load_evaluator(Path(task_path), evaluator_entrypoint)
            result = _normalize_evaluation(evaluate(input_path, output_path))
        connection.send(("ok", result))
    except InvalidOptimizationOutput as exc:
        connection.send(("invalid", str(exc)[:MAX_DIAGNOSTIC_CHARS]))
    except BaseException as exc:  # noqa: BLE001 - report all evaluator failures to the parent
        try:
            connection.send(("error", f"{type(exc).__name__}: {exc}"[:MAX_DIAGNOSTIC_CHARS]))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _kill_evaluator(process: Any) -> None:
    if process.pid is None:
        return
    try:
        if os.name == "posix":
            os.kill(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _evaluate(
    task: Path,
    evaluator_entrypoint: str,
    input_path: Path,
    output_path: Path,
    config: OptimizationConfig,
) -> dict[str, Any]:
    """Run and validate the author evaluator in a short-lived worker.

    Evaluators are trusted task code, but they still cannot be allowed to hang
    the judge process or print unbounded diagnostics.  A fresh process also
    prevents evaluator globals from leaking from one test instance to the
    next.  The budget is deliberately separate from contestant output: the
    evaluator gets at least one second and at most twice the task time limit.
    """

    evaluator_timeout_ms = max(1_000, config.time_limit_ms * 2)
    # Spawn avoids inheriting the evaluator worker's threads and mutable
    # process state.  The child receives paths rather than a dynamic function,
    # so this remains portable across Linux, macOS, and Windows.
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_evaluation_worker,
        args=(
            str(task),
            evaluator_entrypoint,
            str(input_path),
            str(output_path),
            child,
            config.memory_limit_mb,
            evaluator_timeout_ms,
        ),
    )
    process.start()
    child.close()
    try:
        if not parent.poll(evaluator_timeout_ms / 1000):
            _kill_evaluator(process)
            process.join(timeout=1)
            raise OptimizationJudgeError("optimization evaluator exceeded its time limit")
        try:
            kind, payload = parent.recv()
        except (EOFError, OSError) as exc:
            raise OptimizationJudgeError("optimization evaluator terminated without a result") from exc
        process.join(timeout=1)
        if kind == "invalid":
            raise InvalidOptimizationOutput(str(payload))
        if kind == "error":
            raise OptimizationJudgeError(f"optimization evaluator failed: {payload}")
        if kind != "ok" or not isinstance(payload, dict):
            raise OptimizationJudgeError("optimization evaluator returned an invalid result")
        return payload
    finally:
        parent.close()
        if process.is_alive():
            _kill_evaluator(process)
        process.join(timeout=1)


def _run_solution(
    command: list[str],
    input_path: Path,
    output_path: Path,
    config: OptimizationConfig,
    *,
    sandbox: bool = True,
    drop_privileges: bool | None = None,
) -> Any:
    if drop_privileges is None:
        drop_privileges = os.environ.get("BRUNOST_JUDGE_CLASSIC_DROP_PRIVILEGES", "false").lower() == "true"
    return _run_process(
        command,
        cwd=output_path.parent,
        stdin_path=input_path,
        stdout_path=output_path,
        timeout_ms=config.time_limit_ms,
        memory_mb=config.memory_limit_mb,
        output_limit_bytes=config.output_limit_bytes,
        sandbox=sandbox,
        drop_privileges=drop_privileges,
    )


def _baseline_ratio(candidate: float, baseline: float, direction: str) -> float:
    if candidate < 0 or baseline < 0:
        raise OptimizationJudgeError("baseline-ratio scoring requires non-negative objectives")
    if direction == "maximize":
        if baseline == 0:
            return 1.0 if candidate >= baseline else 0.0
        return max(0.0, min(1.0, candidate / baseline))
    if candidate == 0:
        return 1.0
    if baseline == 0:
        return 1.0 if candidate <= baseline else 0.0
    return max(0.0, min(1.0, baseline / candidate))


def _aggregate(scores: list[float], aggregation: str) -> float:
    if not scores:
        return 0.0
    if aggregation == "minimum":
        return min(scores)
    if aggregation == "geometric_mean":
        if any(score <= 0 for score in scores):
            return 0.0
        return math.prod(scores) ** (1.0 / len(scores))
    return sum(scores) / len(scores)


def run_optimization(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Run a candidate program against every optimization instance."""

    submission = Path(submission_path).resolve()
    task = Path(assets_path).resolve()
    try:
        if not submission.is_dir() or not task.is_dir():
            raise OptimizationJudgeError("submission and task must be directories")
        config = _config(task)
        inputs = _inputs(task)
        judge_config = ClassicConfig(
            kind="optimization",
            language=config.language,
            entrypoint=config.entrypoint,
            interactor="",
            time_limit_ms=config.time_limit_ms,
            memory_limit_mb=config.memory_limit_mb,
            output_limit_bytes=config.output_limit_bytes,
            answer_source="answer_key",
            scoring_mode="percentage",
            reference_language=config.language,
            reference_entrypoint=None,
        )
        with _temporary_workspace(prefix="brunost-optimization-") as temporary, _temporary_workspace(
            prefix="brunost-optimization-baseline-"
        ) as baseline_temporary:
            root = Path(temporary)
            # Interpreters must be able to resolve their CWD after the UID
            # drop, so this root is searchable but not listable.  Private task
            # inputs and baseline artifacts stay under distinct 0700 roots;
            # _compile narrows only the candidate child workspace.
            root.chmod(0o711)
            candidate_dir = root / "candidate"
            candidate_dir.mkdir()
            source = _source_path(submission, judge_config)
            try:
                candidate_command, compile_stderr = _compile(source, judge_config, candidate_dir)
            except _CompileFailure as exc:
                return {
                    "status": "completed",
                    "score": 0.0,
                    "metrics": {
                        "runner": "optimization",
                        "schema_version": 1,
                        "language": config.language,
                        "verdict": "CE",
                        "tests": [],
                        "feasible_tests": 0,
                        "total_tests": len(inputs),
                        "instance_count": 0,
                        "limits": {
                            "time_limit_ms": config.time_limit_ms,
                            "memory_limit_mb": config.memory_limit_mb,
                            "output_limit_bytes": config.output_limit_bytes,
                        },
                        "compile_stderr": exc.message[:MAX_DIAGNOSTIC_CHARS],
                    },
                }

            baseline_values: list[float | None] = [None] * len(inputs)
            if config.baseline_entrypoint:
                baseline_path = _safe_relative(task, config.baseline_entrypoint)
                if not baseline_path.is_file():
                    raise OptimizationJudgeError(f"optimization baseline does not exist: {config.baseline_entrypoint}")
                baseline_dir = Path(baseline_temporary) / "work"
                baseline_dir.mkdir()
                baseline_dir.chmod(0o700)
                with _temporary_workspace(prefix="brunost-optimization-baseline-compile-") as compile_temporary:
                    compile_root = Path(compile_temporary)
                    compile_root.chmod(0o711)
                    compile_dir = compile_root / "work"
                    compile_dir.mkdir()
                    baseline_command, _ = _compile(baseline_path, judge_config, compile_dir)
                    _copy_compiled_runtime(baseline_path, compile_dir, baseline_dir)
                for index, input_path in enumerate(inputs):
                    output_path = baseline_dir / f"output-{index}.txt"
                    outcome = _run_solution(
                        baseline_command,
                        input_path,
                        output_path,
                        config,
                        sandbox=False,
                        drop_privileges=False,
                    )
                    if outcome.verdict != "OK":
                        raise OptimizationJudgeError(f"baseline failed on {input_path.name}: {outcome.verdict}")
                    result = _evaluate(task, config.evaluator_entrypoint, input_path, output_path, config)
                    if not result["feasible"] or result["objective"] is None:
                        raise OptimizationJudgeError(f"baseline is infeasible on {input_path.name}")
                    baseline_values[index] = float(result["objective"])

            rows: list[dict[str, Any]] = []
            scores: list[float] = []
            for index, input_path in enumerate(inputs):
                output_path = candidate_dir / f"output-{index}.txt"
                outcome = _run_solution(candidate_command, input_path, output_path, config)
                row: dict[str, Any] = {
                    "id": input_path.relative_to(task / "tests").with_suffix("").as_posix(),
                    "verdict": outcome.verdict,
                    "time_ms": round(outcome.elapsed_ms, 3),
                    "output_bytes": outcome.output_bytes,
                }
                if outcome.stderr:
                    row["message"] = outcome.stderr[:1000]
                if outcome.verdict == "OK":
                    try:
                        result = _evaluate(task, config.evaluator_entrypoint, input_path, output_path, config)
                    except InvalidOptimizationOutput as exc:
                        row["verdict"] = "INVALID"
                        row["message"] = str(exc)[:1000]
                        scores.append(0.0)
                        rows.append(row)
                        continue
                    row["feasible"] = result["feasible"]
                    if result["objective"] is not None:
                        row["objective"] = round(float(result["objective"]), 8)
                    if result["message"]:
                        row["message"] = result["message"]
                    if not result["feasible"]:
                        row["verdict"] = "INFEASIBLE"
                        scores.append(0.0)
                    elif config.score_mode == "baseline_ratio":
                        baseline = baseline_values[index]
                        if baseline is None:
                            raise OptimizationJudgeError("baseline objective is missing")
                        score = _baseline_ratio(float(result["objective"]), baseline, config.objective_direction)
                        row["score"] = round(score, 8)
                        scores.append(score)
                    else:
                        score = result["score"]
                        if score is None or not 0.0 <= score <= 1.0:
                            raise OptimizationJudgeError("checker_score tasks need evaluator score between 0 and 1")
                        row["score"] = round(score, 8)
                        scores.append(score)
                else:
                    scores.append(0.0)
                rows.append(row)

            score = _aggregate(scores, config.aggregation)
            verdict = "OK" if all(row["verdict"] == "OK" for row in rows) else next(
                (row["verdict"] for row in rows if row["verdict"] not in {"OK"}),
                "FAILED",
            )
            return {
                "status": "completed",
                "score": round(score, 8),
                "metrics": {
                    "runner": "optimization",
                    "schema_version": 1,
                    "language": config.language,
                    "objective_direction": config.objective_direction,
                    "score_mode": config.score_mode,
                    "aggregation": config.aggregation,
                    "verdict": verdict,
                    "tests": rows,
                    "instance_count": len(rows),
                    "feasible_tests": sum(row.get("feasible") is True for row in rows),
                    "total_tests": len(rows),
                    "limits": {
                        "time_limit_ms": config.time_limit_ms,
                        "memory_limit_mb": config.memory_limit_mb,
                        "output_limit_bytes": config.output_limit_bytes,
                    },
                    **({"compile_stderr": compile_stderr[:MAX_DIAGNOSTIC_CHARS]} if compile_stderr else {}),
                },
            }
    except OptimizationJudgeError as exc:
        return _failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - evaluator failures must be terminal and explicit
        return _failed(f"optimization judge failure: {type(exc).__name__}: {exc}")
