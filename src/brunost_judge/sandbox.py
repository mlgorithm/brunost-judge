"""Execution boundaries for untrusted task scorers.

The process runner is convenient for local development. Official deployments
should select the Docker runner with a gVisor/Kata runtime. Docker arguments are
always passed as argv items; submission mounts are read-only and private task
data is streamed into the evaluator.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from brunost_judge.artifacts import pack_directory
from brunost_judge.conformance import validate_runner_result_payload
from grader.harness import run


class SandboxRunner(Protocol):
    def run(self, submission: Path, task: Path, execution_id: str) -> dict[str, Any]: ...


def _artifact_limit() -> int:
    try:
        return max(1, int(os.environ.get("BRUNOST_JUDGE_RESULT_ARTIFACT_MAX_BYTES", str(64 * 1024 * 1024))))
    except ValueError:
        return 64 * 1024 * 1024


def collect_result_artifacts(result: dict[str, Any], output_root: Path) -> dict[str, Any]:
    """Package runner-declared files for the worker's content-addressed store."""

    declarations = result.get("artifacts") or {}
    if not declarations:
        return result
    artifact_root = (output_root / "artifacts").resolve()
    payloads: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for name, descriptor in declarations.items():
        path_value = descriptor.get("path") if isinstance(descriptor, dict) else descriptor
        if not isinstance(path_value, str):
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"result artifact {name!r} has no path"}
        target = (artifact_root / path_value).resolve()
        if target != artifact_root and artifact_root not in target.parents:
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"result artifact {name!r} escapes output_path"}
        if not target.exists():
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"result artifact {name!r} was not created"}
        if target.is_dir():
            package_root = target
            size = sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
            filename = target.name
        elif target.is_file():
            package_root = Path(tempfile.mkdtemp(prefix="brunost-result-artifact-"))
            filename = target.name
            shutil.copyfile(target, package_root / filename)
            size = target.stat().st_size
        else:
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"result artifact {name!r} is not a file or directory"}
        try:
            if size > _artifact_limit():
                return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"result artifact {name!r} exceeds configured size limit"}
            total_bytes += size
            if total_bytes > _artifact_limit() * 4:
                return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": "result artifacts exceed configured total size limit"}
            payloads[name] = {
                "data": pack_directory(package_root),
                "media_type": descriptor.get("media_type") if isinstance(descriptor, dict) else None,
                "kind": descriptor.get("kind") if isinstance(descriptor, dict) else None,
                "filename": filename,
            }
        finally:
            if target.is_file():
                shutil.rmtree(package_root, ignore_errors=True)
    result = dict(result)
    result["_artifact_payloads"] = payloads
    return result


class _SandboxTimeout(BaseException):
    pass


def _run_with_timeout(callback, timeout_seconds: int):  # type: ignore[no-untyped-def]
    if timeout_seconds <= 0 or threading.current_thread() is not threading.main_thread():
        return callback()

    def raise_timeout(_signum, _frame):  # type: ignore[no-untyped-def]
        raise _SandboxTimeout(f"sandbox timed out after {timeout_seconds}s")

    previous = signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return callback()
    except _SandboxTimeout:
        return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"sandbox timed out after {timeout_seconds}s"}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class ProcessSandboxRunner:
    """Development runner; task code executes in the worker process."""

    timeout_seconds: int = 900

    def with_timeout(self, timeout_seconds: int | None):
        if timeout_seconds is None:
            return self
        return type(self)(max(1, int(timeout_seconds)))

    def run(self, submission: Path, task: Path, execution_id: str) -> dict[str, Any]:
        _ = execution_id
        with tempfile.TemporaryDirectory(prefix="brunost-process-output-") as output_dir:
            artifact_path = str(Path(output_dir) / "artifacts")
            previous = os.environ.get("RESULT_ARTIFACTS_PATH")
            os.environ["RESULT_ARTIFACTS_PATH"] = artifact_path
            try:
                result = _run_with_timeout(lambda: run(str(submission), str(task)), self.timeout_seconds)
            finally:
                if previous is None:
                    os.environ.pop("RESULT_ARTIFACTS_PATH", None)
                else:
                    os.environ["RESULT_ARTIFACTS_PATH"] = previous
            return collect_result_artifacts(result, Path(output_dir))


@dataclass(frozen=True)
class DockerSandboxRunner:
    image: str
    runtime: str
    timeout_seconds: int = 900
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 256
    seccomp_profile: str | None = None

    def run(self, submission: Path, task: Path, execution_id: str) -> dict[str, Any]:
        if shutil.which("docker") is None:
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": "docker CLI is unavailable"}
        submission = submission.resolve()
        task = task.resolve()
        if not submission.is_dir() or not task.is_dir():
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": "sandbox mount path is not a directory"}

        with tempfile.TemporaryDirectory(prefix="brunost-judge-output-") as output_dir:
            try:
                task_bundle = pack_directory(task)
            except (OSError, ValueError) as exc:
                return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": str(exc)}
            output = Path(output_dir)
            output.chmod(0o777)
            (output / "artifacts").mkdir()
            (output / "artifacts").chmod(0o777)
            label = f"brunost.judge.execution={execution_id}"
            command = [
                "docker", "run", "--rm", "--interactive", "--label", label,
                "--runtime", self.runtime, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
                "--security-opt", "no-new-privileges",
                "--init", "--ulimit", "core=0", "--ulimit", "nofile=1024:1024",
                "--pids-limit", str(self.pids_limit), "--memory", self.memory,
                "--cpus", self.cpus, "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--tmpfs", "/dev/shm:rw,noexec,nosuid,nodev,size=64m",
                # The evaluator needs root only to read the root-only task tmpfs
                # and drop the contestant process to dedicated UID 65533. The evaluator
                # container still has no network, no filesystem write access,
                # and no capabilities beyond that controlled UID transition.
                "--user", "0:0",
                "--volume", f"{submission}:/workspace/submission:ro",
                "--volume", f"{output}:/workspace/output:rw",
                "--env", "SUBMISSION_PATH=/workspace/submission",
                "--env", "BRUNOST_JUDGE_TASK_BUNDLE=stdin",
                "--env", "RESULT_PATH=/workspace/output/results.json",
                "--env", "RESULT_ARTIFACTS_PATH=/workspace/output/artifacts",
                "--env", "HOME=/tmp",
                # The task bundle is delivered on evaluator stdin and extracted
                # into root-only tmpfs; it is never mounted into the container.
                "--env", "BRUNOST_JUDGE_CLASSIC_USE_BWRAP=false",
                "--env", "BRUNOST_JUDGE_CLASSIC_DROP_PRIVILEGES=true",
            ]
            if self.seccomp_profile:
                command.extend(["--security-opt", f"seccomp={self.seccomp_profile}"])
            plugin_module = os.environ.get("BRUNOST_JUDGE_RUNNER_PLUGIN_MODULE", "").strip()
            if plugin_module:
                command.extend(["--env", f"BRUNOST_JUDGE_RUNNER_PLUGIN_MODULE={plugin_module}"])
            command.extend([self.image, "python", "-m", "grader.evaluate"])
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    input=task_bundle,
                    timeout=max(1, self.timeout_seconds),
                )
            except subprocess.TimeoutExpired:
                self._cleanup(label)
                return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"sandbox timed out after {self.timeout_seconds}s"}
            result_path = output / "results.json"
            if result_path.is_file():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(result, dict):
                        errors = validate_runner_result_payload(result)
                        if errors:
                            return {
                                "status": "failed",
                                "score": 0.0,
                                "metrics": {},
                                "failure_reason": "invalid sandbox result: " + "; ".join(errors),
                            }
                        if completed.returncode != 0 and result.get("status") == "completed":
                            result["status"] = "failed"
                            result["failure_reason"] = "sandbox exited unsuccessfully"
                        return collect_result_artifacts(result, output)
                except (OSError, json.JSONDecodeError):
                    pass
            raw_detail = completed.stderr or completed.stdout or b"sandbox produced no result"
            detail = raw_detail.decode("utf-8", errors="replace") if isinstance(raw_detail, bytes) else str(raw_detail)
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"sandbox exit {completed.returncode}: {detail[:1800]}"}

    def with_timeout(self, timeout_seconds: int | None):
        if timeout_seconds is None:
            return self
        return type(self)(
            image=self.image,
            runtime=self.runtime,
            timeout_seconds=max(1, int(timeout_seconds)),
            memory=self.memory,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            seccomp_profile=self.seccomp_profile,
        )

    @staticmethod
    def _cleanup(label: str) -> None:
        containers = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={label}"], check=False, capture_output=True, text=True).stdout.split()
        if containers:
            subprocess.run(["docker", "rm", "-f", *containers], check=False, capture_output=True)


def sandbox_from_environment() -> SandboxRunner:
    production = os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production", "staging"}
    configured_mode = os.environ.get("BRUNOST_JUDGE_SANDBOX_MODE", "").strip().lower()
    if production and not configured_mode:
        raise RuntimeError("production requires explicit BRUNOST_JUDGE_SANDBOX_MODE=docker")
    mode = configured_mode or "process"
    if mode in {"process", "local", "development"}:
        if production:
            raise RuntimeError("the in-process sandbox is unavailable in production")
        return ProcessSandboxRunner()
    if mode != "docker":
        raise ValueError(f"unsupported BRUNOST_JUDGE_SANDBOX_MODE: {mode}")
    image = os.environ.get("BRUNOST_JUDGE_SANDBOX_IMAGE", "").strip()
    runtime = os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "").strip()
    seccomp_profile = os.environ.get("BRUNOST_JUDGE_SANDBOX_SECCOMP", "").strip()
    if not image:
        raise RuntimeError("BRUNOST_JUDGE_SANDBOX_IMAGE is required for Docker sandbox mode")
    if not runtime:
        raise RuntimeError("BRUNOST_JUDGE_SANDBOX_RUNTIME is required for Docker sandbox mode")
    if production and not re.fullmatch(r"[^\s@]+@sha256:[0-9a-fA-F]{64}", image):
        raise RuntimeError("production sandbox image must be pinned by a sha256 digest")
    if os.environ.get("BRUNOST_JUDGE_REQUIRE_SECCOMP", "true").lower() == "true" and not seccomp_profile:
        raise RuntimeError("BRUNOST_JUDGE_SANDBOX_SECCOMP is required for Docker sandbox mode")
    return DockerSandboxRunner(
        image=image,
        runtime=runtime,
        timeout_seconds=int(os.environ.get("BRUNOST_JUDGE_SANDBOX_TIMEOUT_SECONDS", "900")),
        memory=os.environ.get("BRUNOST_JUDGE_SANDBOX_MEMORY", "4g"),
        cpus=os.environ.get("BRUNOST_JUDGE_SANDBOX_CPUS", "2"),
        pids_limit=int(os.environ.get("BRUNOST_JUDGE_SANDBOX_PIDS_LIMIT", "256")),
        seccomp_profile=seccomp_profile or None,
    )
