"""Classic batch judge for ICPC/IOI-style task packages.

The runner is deliberately dependency-free. A task package contains:

* ``judge.yaml`` with flat runner settings;
* ``tests/**/*.in`` and matching ``.ans`` (or ``.out``) answer files;
* an optional ``subtasks.json`` assigning tests to point-bearing subtasks; and
* an optional ``checker.py`` exposing ``check(input, answer, output)``.

This module runs inside the evaluator sandbox. The process-mode runner is only
for local development; production must use the Docker/gVisor/Kata sandbox.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_DIAGNOSTIC_CHARS = 4000
DEFAULT_TIME_LIMIT_MS = 2000
DEFAULT_MEMORY_LIMIT_MB = 512
DEFAULT_OUTPUT_LIMIT_BYTES = 1 << 20
COMPILE_TIMEOUT_MS = 30_000
CONTESTANT_UID = 65533
CONTESTANT_GID = 65533


class ClassicJudgeError(ValueError):
    """A task/package/evaluator error, not a contestant verdict."""


@dataclass(frozen=True)
class ClassicConfig:
    kind: str
    language: str
    entrypoint: str | None
    interactor: str
    time_limit_ms: int
    memory_limit_mb: int
    output_limit_bytes: int


@dataclass(frozen=True)
class TestCase:
    test_id: str
    input_path: Path
    answer_path: Path
    subtask_id: str


@dataclass(frozen=True)
class Subtask:
    subtask_id: str
    points: float
    tests: tuple[TestCase, ...]


@dataclass(frozen=True)
class ProcessOutcome:
    verdict: str
    elapsed_ms: float
    output_bytes: int
    stderr: str


def _failed(reason: str, *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "failed",
        "score": 0.0,
        "metrics": metrics or {"runner": "classic"},
        "failure_reason": reason[:MAX_DIAGNOSTIC_CHARS],
    }


def _completed(
    *,
    score: float,
    verdict: str,
    language: str,
    tests: list[dict[str, Any]],
    subtasks: list[dict[str, Any]],
    compile_stderr: str = "",
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "runner": "classic",
        "language": language,
        "verdict": verdict,
        "tests": tests,
        "subtasks": subtasks,
        "points": sum(float(item.get("awarded", 0.0)) for item in subtasks),
        "points_max": sum(float(item.get("points", 0.0)) for item in subtasks),
    }
    if compile_stderr:
        metrics["compile_stderr"] = compile_stderr[:MAX_DIAGNOSTIC_CHARS]
    return {"status": "completed", "score": float(score), "metrics": metrics}


def _manifest(path: Path) -> dict[str, str]:
    """Read the intentionally small flat YAML subset used by task manifests."""
    result: dict[str, str] = {}
    manifest_path = path / "judge.yaml"
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ClassicJudgeError(f"cannot read judge.yaml: {exc}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not key.startswith("-"):
            result[key] = value
    return result


def _positive_int(value: str | None, default: int, name: str) -> int:
    try:
        result = int(value) if value is not None else default
    except ValueError as exc:
        raise ClassicJudgeError(f"{name} must be an integer") from exc
    if result < 1:
        raise ClassicJudgeError(f"{name} must be positive")
    return result


def _config(task: Path) -> ClassicConfig:
    values = _manifest(task)
    kind = values.get("kind", "").lower()
    if kind not in {"icpc", "ioi", "interactive"}:
        raise ClassicJudgeError(f"classic runner does not support task kind {kind!r}")
    if values.get("version") != "1":
        raise ClassicJudgeError("classic tasks must declare version: 1")
    if values.get("runner", "classic").lower() != "classic":
        raise ClassicJudgeError("classic task must declare runner: classic")
    language = values.get("language", "").lower()
    if language in {"gnu++17", "c++17", "g++"}:
        language = "cpp"
    else:
        language = language.replace("gnu++", "cpp")
    if language not in {"python", "py", "cpp", "c++", "c", "rust", "rs"}:
        raise ClassicJudgeError("language must be python, c, cpp, or rust")
    if language == "py":
        language = "python"
    if language == "c++":
        language = "cpp"
    if language == "rs":
        language = "rust"
    return ClassicConfig(
        kind=kind,
        language=language,
        entrypoint=values.get("entrypoint") or None,
        interactor=values.get("interactor", "interactor.py"),
        time_limit_ms=_positive_int(values.get("time_limit_ms"), DEFAULT_TIME_LIMIT_MS, "time_limit_ms"),
        memory_limit_mb=_positive_int(values.get("memory_limit_mb"), DEFAULT_MEMORY_LIMIT_MB, "memory_limit_mb"),
        output_limit_bytes=_positive_int(
            values.get("output_limit_bytes"), DEFAULT_OUTPUT_LIMIT_BYTES, "output_limit_bytes"
        ),
    )


def _safe_relative(root: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ClassicJudgeError("entrypoint must stay inside the submission directory")
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ClassicJudgeError("entrypoint must stay inside the submission directory")
    return resolved


def _source_path(submission: Path, config: ClassicConfig) -> Path:
    if config.entrypoint:
        source = _safe_relative(submission, config.entrypoint)
        if not source.is_file():
            raise ClassicJudgeError(f"submission entrypoint does not exist: {config.entrypoint}")
        return source
    extensions = {
        "python": (".py",),
        "c": (".c",),
        "cpp": (".cc", ".cpp", ".cxx"),
        "rust": (".rs",),
    }[config.language]
    candidates = sorted(item for item in submission.rglob("*") if item.is_file() and item.suffix in extensions)
    if not candidates:
        raise ClassicJudgeError(f"submission contains no {config.language} source file")
    return candidates[0]


def _test_cases(task: Path) -> tuple[TestCase, ...]:
    tests_root = task / "tests"
    if not tests_root.is_dir():
        raise ClassicJudgeError("classic task is missing tests/")
    cases: list[TestCase] = []
    for input_path in sorted(tests_root.rglob("*.in")):
        relative = input_path.relative_to(tests_root)
        test_id = relative.with_suffix("").as_posix()
        answer_path = input_path.with_suffix(".ans")
        if not answer_path.is_file():
            answer_path = input_path.with_suffix(".out")
        if not answer_path.is_file():
            raise ClassicJudgeError(f"test {test_id} is missing .ans or .out")
        subtask_id = relative.parts[0] if len(relative.parts) > 1 else "default"
        cases.append(TestCase(test_id, input_path, answer_path, subtask_id))
    if not cases:
        raise ClassicJudgeError("classic task has no tests/*.in files")
    return tuple(cases)


def _subtasks(task: Path, cases: tuple[TestCase, ...]) -> tuple[Subtask, ...]:
    config_path = task / "subtasks.json"
    if not config_path.is_file():
        return (Subtask("default", 100.0, cases),)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClassicJudgeError(f"invalid subtasks.json: {exc}") from exc
    rows = payload.get("subtasks") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ClassicJudgeError("subtasks.json must contain a non-empty subtasks list")
    by_id = {case.test_id: case for case in cases}
    assigned: set[str] = set()
    subtasks: list[Subtask] = []
    total_points = 0.0
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not isinstance(row.get("tests"), list):
            raise ClassicJudgeError("each subtask needs id, points, and tests")
        subtask_id = str(row["id"])
        try:
            points = float(row["points"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ClassicJudgeError(f"subtask {subtask_id} has invalid points") from exc
        if not math.isfinite(points) or points <= 0:
            raise ClassicJudgeError(f"subtask {subtask_id} points must be positive")
        selected: list[TestCase] = []
        for raw_id in row["tests"]:
            test_id = str(raw_id)
            case = by_id.get(test_id)
            if case is None:
                raise ClassicJudgeError(f"subtask {subtask_id} references unknown test {test_id}")
            if test_id in assigned:
                raise ClassicJudgeError(f"test {test_id} belongs to more than one subtask")
            assigned.add(test_id)
            selected.append(case)
        if not selected:
            raise ClassicJudgeError(f"subtask {subtask_id} has no tests")
        total_points += points
        subtasks.append(Subtask(subtask_id, points, tuple(selected)))
    if assigned != set(by_id):
        missing = sorted(set(by_id) - assigned)
        raise ClassicJudgeError(f"subtasks.json does not assign tests: {', '.join(missing)}")
    if total_points <= 0:
        raise ClassicJudgeError("subtask points must sum to a positive value")
    return tuple(subtasks)


def _limit_child(memory_mb: int, time_limit_ms: int, output_limit_bytes: int | None) -> None:
    if os.name != "posix":
        return
    import resource

    def set_limit(kind: int, value: int) -> None:
        try:
            resource.setrlimit(kind, (value, value))
        except (OSError, ValueError):
            # RLIMIT_AS is unavailable on some macOS kernels. The production
            # container still enforces memory with its cgroup/VM limit.
            return

    cpu_seconds = max(1, math.ceil(time_limit_ms / 1000) + 1)
    set_limit(resource.RLIMIT_CPU, cpu_seconds)
    set_limit(resource.RLIMIT_AS, memory_mb * 1024 * 1024)
    if output_limit_bytes is not None:
        set_limit(resource.RLIMIT_FSIZE, output_limit_bytes)


def _child_setup(
    memory_mb: int,
    time_limit_ms: int,
    output_limit_bytes: int | None,
    drop_privileges: bool = False,
) -> None:
    _limit_child(memory_mb, time_limit_ms, output_limit_bytes)
    if not drop_privileges or os.name != "posix":
        return
    if os.geteuid() != 0:
        raise PermissionError("classic privilege dropping requires a root evaluator process")
    os.setgroups([])
    os.setgid(CONTESTANT_GID)
    os.setuid(CONTESTANT_UID)


def _kill_process(process: subprocess.Popen[Any]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _sandbox_command(command: list[str], cwd: Path) -> list[str]:
    """Hide the evaluator's task mount from contestant/compiler processes."""
    if os.environ.get("BRUNOST_JUDGE_CLASSIC_USE_BWRAP", "false").lower() != "true":
        return command
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise OSError("bwrap is required for the classic production sandbox")
    mapped: list[str] = []
    for argument in command:
        try:
            relative = Path(argument).relative_to(cwd)
        except (TypeError, ValueError):
            mapped.append(argument)
        else:
            mapped.append(f"/work/{relative.as_posix()}")
    wrapped = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--clearenv",
        "--dir",
        "/work",
        "--bind",
        str(cwd),
        "/work",
        "--chdir",
        "/work",
        "--proc",
        "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--uid",
            str(CONTESTANT_UID),
            "--gid",
            str(CONTESTANT_GID),
    ]
    for root in ("/bin", "/etc", "/lib", "/lib64", "/usr", "/usr/local"):
        if Path(root).exists():
            wrapped.extend(["--ro-bind", root, root])
    wrapped.extend(
        [
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "LANG",
            "C",
            "--setenv",
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            *mapped,
        ]
    )
    return wrapped


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    stdin_path: Path | None,
    stdout_path: Path | None,
    timeout_ms: int,
    memory_mb: int,
    output_limit_bytes: int | None,
    sandbox: bool = True,
    extra_env: dict[str, str] | None = None,
    drop_privileges: bool = False,
) -> ProcessOutcome:
    started = time.monotonic()
    stderr_file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - closed in finally below
    stdout_file = stdout_path.open("wb") if stdout_path else None
    stdin_file = stdin_path.open("rb") if stdin_path else None
    try:
        try:
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": "/tmp",
                "LANG": "C",
                **({"PYTHONPATH": os.environ["PYTHONPATH"]} if os.environ.get("PYTHONPATH") else {}),
                **(extra_env or {}),
            }
            process = subprocess.Popen(
                _sandbox_command(command, cwd) if sandbox else command,
                cwd=str(cwd),
                stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
                stdout=stdout_file if stdout_file is not None else subprocess.DEVNULL,
                stderr=stderr_file,
                env=environment,
                start_new_session=os.name == "posix",
                # The child must receive rlimits before it can execute user code.
                preexec_fn=(lambda: _child_setup(memory_mb, timeout_ms, output_limit_bytes, drop_privileges))  # noqa: PLW1509
                if os.name == "posix"
                else None,
            )
        except OSError as exc:
            return ProcessOutcome("judge_error", 0.0, 0, str(exc))
        timed_out = False
        output_limited = False
        while process.poll() is None:
            if (
                stdout_path
                and output_limit_bytes is not None
                and stdout_path.exists()
                and stdout_path.stat().st_size > output_limit_bytes
            ):
                output_limited = True
                _kill_process(process)
                break
            if (time.monotonic() - started) * 1000 > timeout_ms:
                timed_out = True
                _kill_process(process)
                break
            time.sleep(0.01)
        process.wait()
        elapsed_ms = (time.monotonic() - started) * 1000
        output_bytes = stdout_path.stat().st_size if stdout_path and stdout_path.exists() else 0
        stderr_file.seek(0)
        stderr = stderr_file.read(MAX_DIAGNOSTIC_CHARS).decode("utf-8", errors="replace")
        if output_limited or (
            stdout_path and output_limit_bytes is not None and output_bytes > output_limit_bytes
        ):
            verdict = "OLE"
        elif timed_out:
            verdict = "TLE"
        elif (
            process.returncode != 0
            and stdout_path
            and output_limit_bytes is not None
            and output_bytes >= output_limit_bytes
        ):
            verdict = "OLE"
        elif process.returncode != 0:
            verdict = "RE"
        else:
            verdict = "OK"
        return ProcessOutcome(verdict, elapsed_ms, output_bytes, stderr)
    finally:
        stderr_file.close()
        if stdout_file is not None:
            stdout_file.close()
        if stdin_file is not None:
            stdin_file.close()


def _compile(source: Path, config: ClassicConfig, build_dir: Path) -> tuple[list[str], str]:
    build_dir.chmod(0o755)
    staged_source = build_dir / source.name
    shutil.copy2(source, staged_source)
    if config.language == "python":
        return [sys.executable, str(staged_source)], ""
    compiler_name = {"c": "gcc", "cpp": "g++", "rust": "rustc"}[config.language]
    compiler = os.environ.get("BRUNOST_JUDGE_" + compiler_name.upper(), compiler_name)
    if not shutil.which(compiler):
        raise ClassicJudgeError(f"required compiler is unavailable: {compiler}")
    binary = build_dir / "program"
    if config.language == "c":
        command = [compiler, "-std=c11", "-O2", "-pipe", str(staged_source), "-o", str(binary)]
    elif config.language == "cpp":
        command = [compiler, "-std=c++17", "-O2", "-pipe", str(staged_source), "-o", str(binary)]
    else:
        command = [compiler, "-O", "-o", str(binary), str(staged_source)]
    outcome = _run_process(
        command,
        cwd=build_dir,
        stdin_path=None,
        stdout_path=None,
        timeout_ms=COMPILE_TIMEOUT_MS,
        memory_mb=2048,
        output_limit_bytes=None,
    )
    if outcome.verdict == "judge_error":
        raise ClassicJudgeError(f"compiler could not start: {outcome.stderr}")
    if outcome.verdict != "OK":
        raise _CompileFailure(outcome.verdict, outcome.stderr)
    return [str(binary)], outcome.stderr


class _CompileFailure(Exception):
    def __init__(self, verdict: str, message: str) -> None:
        self.verdict = verdict
        self.message = message


def _load_checker(task: Path):
    checker_path = task / "checker.py"
    if not checker_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("classic_task_checker", checker_path)
    if spec is None or spec.loader is None:
        raise ClassicJudgeError("checker.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    sys.path.insert(0, str(task))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    check = getattr(module, "check", None)
    if not callable(check):
        raise ClassicJudgeError("checker.py must define check(input_path, answer_path, output_path)")
    return check


def _check_output(checker, case: TestCase, output_path: Path) -> tuple[bool, str]:
    if checker is None:
        accepted = output_path.read_bytes().split() == case.answer_path.read_bytes().split()
        return accepted, "" if accepted else "wrong answer"
    raw = checker(str(case.input_path), str(case.answer_path), str(output_path))
    if isinstance(raw, bool):
        return raw, "" if raw else "wrong answer"
    if isinstance(raw, dict):
        verdict = str(raw.get("verdict", "")).upper()
        accepted = bool(raw.get("ok", raw.get("accepted", verdict in {"AC", "OK"})))
        return accepted, str(raw.get("message") or ("" if accepted else "wrong answer"))
    if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
        accepted = float(raw) >= 1.0
        return accepted, "" if accepted else "wrong answer"
    raise ClassicJudgeError("checker returned an unsupported result")


def run_classic(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Compile and judge one classic batch submission."""
    submission = Path(submission_path).resolve()
    task = Path(assets_path).resolve()
    try:
        if not submission.is_dir() or not task.is_dir():
            raise ClassicJudgeError("submission and task must be directories")
        config = _config(task)
        cases = _test_cases(task)
        subtasks = _subtasks(task, cases)
        checker = _load_checker(task)
        source = _source_path(submission, config)
        with tempfile.TemporaryDirectory(prefix="brunost-classic-") as temporary:
            build_dir = Path(temporary)
            try:
                command, compile_stderr = _compile(source, config, build_dir)
            except _CompileFailure as exc:
                compile_metrics = [
                    {
                        "id": subtask.subtask_id,
                        "points": subtask.points,
                        "awarded": 0.0,
                        "verdict": "CE",
                        "tests": [],
                    }
                    for subtask in subtasks
                ]
                return _completed(
                    score=0.0,
                    verdict="CE",
                    language=config.language,
                    tests=[],
                    subtasks=compile_metrics,
                    compile_stderr=exc.message,
                )
            test_metrics: dict[str, dict[str, Any]] = {}
            for case in cases:
                output_path = build_dir / f"output-{len(test_metrics)}.txt"
                outcome = _run_process(
                    command,
                    cwd=build_dir,
                    stdin_path=case.input_path,
                    stdout_path=output_path,
                    timeout_ms=config.time_limit_ms,
                    memory_mb=config.memory_limit_mb,
                    output_limit_bytes=config.output_limit_bytes,
                    drop_privileges=os.environ.get("BRUNOST_JUDGE_CLASSIC_DROP_PRIVILEGES", "false").lower() == "true",
                )
                verdict = outcome.verdict
                message = outcome.stderr
                if verdict == "OK":
                    try:
                        accepted, checker_message = _check_output(checker, case, output_path)
                    except Exception as exc:
                        raise ClassicJudgeError(f"checker failed on {case.test_id}: {type(exc).__name__}: {exc}") from exc
                    if not accepted:
                        verdict = "WA"
                    message = checker_message or message
                test_metrics[case.test_id] = {
                    "id": case.test_id,
                    "subtask": case.subtask_id,
                    "verdict": verdict,
                    "time_ms": round(outcome.elapsed_ms, 3),
                    "output_bytes": outcome.output_bytes,
                    **({"message": message[:1000]} if message else {}),
                }
            subtask_metrics: list[dict[str, Any]] = []
            awarded_total = 0.0
            points_total = 0.0
            for subtask in subtasks:
                rows = [test_metrics[case.test_id] for case in subtask.tests]
                passed = all(row["verdict"] == "OK" for row in rows)
                awarded = subtask.points if passed else 0.0
                awarded_total += awarded
                points_total += subtask.points
                subtask_metrics.append(
                    {
                        "id": subtask.subtask_id,
                        "points": subtask.points,
                        "awarded": awarded,
                        "verdict": "AC" if passed else next(row["verdict"] for row in rows if row["verdict"] != "OK"),
                        "tests": [row["id"] for row in rows],
                    }
                )
            overall = "AC" if awarded_total == points_total else "WA"
            for row in test_metrics.values():
                if row["verdict"] in {"TLE", "OLE", "RE"}:
                    overall = row["verdict"]
                    break
            return _completed(
                score=awarded_total / points_total if points_total else 0.0,
                verdict=overall,
                language=config.language,
                tests=list(test_metrics.values()),
                subtasks=subtask_metrics,
                compile_stderr=compile_stderr,
            )
    except ClassicJudgeError as exc:
        return _failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - evaluator failures must be terminal and explicit
        return _failed(f"classic judge failure: {type(exc).__name__}: {exc}")


def _interactive_inputs(task: Path) -> tuple[Path, ...]:
    inputs = tuple(sorted((task / "tests").rglob("*.in"))) if (task / "tests").is_dir() else ()
    if not inputs:
        raise ClassicJudgeError("interactive tasks need tests/*.in files")
    return inputs


def run_interactive(submission_path: str, assets_path: str) -> dict[str, Any]:
    """Run a line-oriented interactor in a separate evaluator process."""
    submission = Path(submission_path).resolve()
    task = Path(assets_path).resolve()
    try:
        if not submission.is_dir() or not task.is_dir():
            raise ClassicJudgeError("submission and task must be directories")
        config = _config(task)
        if config.kind != "interactive":
            raise ClassicJudgeError("interactive runner requires kind: interactive")
        cases = _interactive_inputs(task)
        with tempfile.TemporaryDirectory(prefix="brunost-interactive-") as temporary:
            build_dir = Path(temporary)
            test_metrics: list[dict[str, Any]] = []
            for index, input_path in enumerate(cases):
                result_path = build_dir / f"interactive-{index}.json"
                command = [
                    sys.executable,
                    "-m",
                    "grader.interactive_worker",
                    str(task),
                    str(submission),
                    str(input_path),
                ]
                outcome = _run_process(
                    command,
                    cwd=build_dir,
                    stdin_path=None,
                    stdout_path=result_path,
                    timeout_ms=config.time_limit_ms,
                    memory_mb=config.memory_limit_mb,
                    output_limit_bytes=config.output_limit_bytes,
                    sandbox=False,
                    extra_env={
                        "PYTHONPATH": os.pathsep.join(
                            part
                            for part in (str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", ""))
                            if part
                        )
                    },
                )
                if outcome.verdict == "TLE":
                    row = {"id": input_path.relative_to(task / "tests").with_suffix("").as_posix(), "verdict": "TLE"}
                elif outcome.verdict != "OK":
                    raise ClassicJudgeError(f"interactive evaluator failed: {outcome.stderr or outcome.verdict}")
                else:
                    try:
                        row = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise ClassicJudgeError(f"interactive evaluator returned invalid JSON: {exc}") from exc
                    if not isinstance(row, dict):
                        raise ClassicJudgeError("interactive evaluator result must be an object")
                    if row.get("status") == "failed":
                        raise ClassicJudgeError(str(row.get("message") or "interactive evaluator failed"))
                row.setdefault("id", input_path.relative_to(task / "tests").with_suffix("").as_posix())
                row.setdefault("verdict", "WA")
                row["time_ms"] = round(outcome.elapsed_ms, 3)
                test_metrics.append(row)
            passed = all(row.get("verdict") == "AC" for row in test_metrics)
            score = sum(float(row.get("score", 1.0 if row.get("verdict") == "AC" else 0.0)) for row in test_metrics)
            score /= len(test_metrics)
            return {
                "status": "completed",
                "score": score,
                "metrics": {
                    "runner": "interactive",
                    "language": config.language,
                    "verdict": "AC" if passed else next(row["verdict"] for row in test_metrics if row.get("verdict") != "AC"),
                    "tests": test_metrics,
                },
            }
    except ClassicJudgeError as exc:
        return _failed(str(exc), metrics={"runner": "interactive"})
    except Exception as exc:  # noqa: BLE001 - evaluator failures must be terminal and explicit
        return _failed(f"interactive judge failure: {type(exc).__name__}: {exc}", metrics={"runner": "interactive"})
