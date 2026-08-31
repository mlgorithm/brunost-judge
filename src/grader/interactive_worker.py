"""Evaluator-side line protocol worker for interactive task packages."""

from __future__ import annotations

import importlib.util
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from grader.classic import (
    MAX_DIAGNOSTIC_CHARS,
    _child_setup,
    _compile,
    _config,
    _kill_process,
    _sandbox_command,
    _source_path,
    _temporary_workspace,
)

MAX_LINE_BYTES = 64 * 1024
MAX_TRANSCRIPT_EVENTS = 2000


class InteractiveProtocolError(ValueError):
    """A candidate/interactor protocol failure."""


class InteractiveSession:
    def __init__(self, process: subprocess.Popen[Any], input_path: Path, time_limit_ms: int, output_limit: int) -> None:
        if process.stdin is None or process.stdout is None:
            raise InteractiveProtocolError("candidate pipes are unavailable")
        self.process = process
        self.input_path = input_path
        self.deadline = time.monotonic() + time_limit_ms / 1000
        self.output_limit = output_limit
        self.output_bytes = 0
        self.transcript: list[dict[str, Any]] = []
        self._buffer = bytearray()
        os.set_blocking(process.stdout.fileno(), False)

    def _record(self, direction: str, value: str) -> None:
        if len(self.transcript) < MAX_TRANSCRIPT_EVENTS:
            self.transcript.append({"direction": direction, "value": value[:MAX_LINE_BYTES]})

    def send(self, value: str) -> None:
        if not isinstance(value, str):
            raise InteractiveProtocolError("session.send expects a string")
        data = value.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        if len(data) > MAX_LINE_BYTES:
            raise InteractiveProtocolError("interactor message exceeds line limit")
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise InteractiveProtocolError("candidate closed its input") from exc
        self._record("judge", value.rstrip("\n"))

    def receive(self) -> str:
        while b"\n" not in self._buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("interactive time limit exceeded")
            readable, _, _ = select.select([self.process.stdout], [], [], min(remaining, 0.05))
            if not readable:
                continue
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise InteractiveProtocolError("candidate closed stdout")
            self._buffer.extend(chunk)
            self.output_bytes += len(chunk)
            if self.output_bytes > self.output_limit:
                raise OverflowError("interactive output limit exceeded")
        raw, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        if len(raw) > MAX_LINE_BYTES:
            raise OverflowError("interactive line limit exceeded")
        value = raw.decode("utf-8", errors="replace")
        self._record("candidate", value)
        return value


def _load_interactor(task: Path, path: str):
    interactor_path = (task / path).resolve()
    if interactor_path != task and task not in interactor_path.parents:
        raise InteractiveProtocolError("interactor must stay inside the task directory")
    if not interactor_path.is_file():
        raise InteractiveProtocolError(f"interactor does not exist: {path}")
    spec = importlib.util.spec_from_file_location("classic_task_interactor", interactor_path)
    if spec is None or spec.loader is None:
        raise InteractiveProtocolError("interactor could not be loaded")
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    sys.path.insert(0, str(task))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    interact = getattr(module, "interact", None)
    if not callable(interact):
        raise InteractiveProtocolError("interactor.py must define interact(session, input_path)")
    return interact


def _result_from_interactor(raw: Any) -> tuple[str, float, str | None]:
    if isinstance(raw, bool):
        return ("AC", 1.0, None) if raw else ("WA", 0.0, "interactor rejected the solution")
    if isinstance(raw, dict):
        verdict = str(raw.get("verdict", "AC" if raw.get("ok") else "WA")).upper()
        if verdict in {"OK", "AC"}:
            verdict = "AC"
        try:
            score = float(raw.get("score", 1.0 if verdict == "AC" else 0.0))
        except (TypeError, ValueError) as exc:
            raise InteractiveProtocolError("interactor score must be numeric") from exc
        if not 0.0 <= score <= 1.0:
            raise InteractiveProtocolError("interactor score must be between 0 and 1")
        return verdict, score, str(raw.get("message")) if raw.get("message") else None
    raise InteractiveProtocolError("interactor must return bool or an object result")


def run(task_path: str, submission_path: str, input_path: str) -> dict[str, Any]:
    task = Path(task_path).resolve()
    submission = Path(submission_path).resolve()
    test_input = Path(input_path).resolve()
    try:
        config = _config(task)
        interactor = _load_interactor(task, config.interactor)
        source = _source_path(submission, config)
        with _temporary_workspace(prefix="brunost-interactive-worker-") as temporary:
            # A searchable/non-listable parent lets an interpreter resolve its
            # CWD after dropping UID, while _compile narrows only this child
            # workspace for the contestant compiler/runtime.
            candidate_root = Path(temporary)
            candidate_root.chmod(0o711)
            build_dir = candidate_root / "work"
            build_dir.mkdir()
            command, compile_stderr = _compile(source, config, build_dir)
            stderr_file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - closed below
            try:
                process = subprocess.Popen(
                    _sandbox_command(command, build_dir),
                    cwd=str(build_dir),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    env={
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "HOME": "/tmp",
                        "LANG": "C",
                    },
                    start_new_session=os.name == "posix",
                    preexec_fn=(  # noqa: PLW1509
                        lambda: _child_setup(
                            config.memory_limit_mb,
                            config.time_limit_ms,
                            None,
                            os.environ.get("BRUNOST_JUDGE_CLASSIC_DROP_PRIVILEGES", "false").lower() == "true",
                        )
                    )
                    if os.name == "posix"
                    else None,
                )
            except OSError as exc:
                return {"status": "failed", "verdict": "JUDGE_ERROR", "message": str(exc)}
            session = InteractiveSession(process, test_input, config.time_limit_ms, config.output_limit_bytes)
            try:
                raw = interactor(session, str(test_input))
                verdict, score, message = _result_from_interactor(raw)
            except TimeoutError as exc:
                _kill_process(process)
                process.wait()
                return {"status": "completed", "verdict": "TLE", "score": 0.0, "message": str(exc)}
            except OverflowError as exc:
                _kill_process(process)
                process.wait()
                return {"status": "completed", "verdict": "OLE", "score": 0.0, "message": str(exc)}
            except InteractiveProtocolError as exc:
                _kill_process(process)
                process.wait()
                return {"status": "completed", "verdict": "RE", "score": 0.0, "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 - task interactor failures are judge failures
                _kill_process(process)
                process.wait()
                return {"status": "failed", "verdict": "JUDGE_ERROR", "message": f"{type(exc).__name__}: {exc}"}
            finally:
                if process.poll() is None:
                    try:
                        process.stdin.close() if process.stdin else None
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        _kill_process(process)
                        process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_DIAGNOSTIC_CHARS).decode("utf-8", errors="replace")
            stderr_file.close()
            if process.returncode not in (0, None) and verdict == "AC":
                verdict, score, message = "RE", 0.0, "candidate exited before the interaction completed"
            return {
                "status": "completed",
                "verdict": verdict,
                "score": score,
                "message": message or stderr or compile_stderr or None,
                "transcript": session.transcript,
            }
    except Exception as exc:  # noqa: BLE001 - worker must return JSON to the evaluator
        return {"status": "failed", "verdict": "JUDGE_ERROR", "message": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: python -m grader.interactive_worker TASK SUBMISSION INPUT", file=sys.stderr)
        return 2
    print(json.dumps(run(sys.argv[1], sys.argv[2], sys.argv[3]), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
