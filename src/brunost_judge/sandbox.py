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
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from brunost_judge.artifacts import pack_directory
from brunost_judge.conformance import validate_runner_result_payload
from grader.harness import run


class SandboxRunner(Protocol):
    def run(self, submission: Path, task: Path, execution_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProcessSandboxRunner:
    """Development runner; task code executes in the worker process."""

    def run(self, submission: Path, task: Path, execution_id: str) -> dict[str, Any]:
        _ = execution_id
        return run(str(submission), str(task))


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
                        return result
                except (OSError, json.JSONDecodeError):
                    pass
            raw_detail = completed.stderr or completed.stdout or b"sandbox produced no result"
            detail = raw_detail.decode("utf-8", errors="replace") if isinstance(raw_detail, bytes) else str(raw_detail)
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"sandbox exit {completed.returncode}: {detail[:1800]}"}

    @staticmethod
    def _cleanup(label: str) -> None:
        containers = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={label}"], check=False, capture_output=True, text=True).stdout.split()
        if containers:
            subprocess.run(["docker", "rm", "-f", *containers], check=False, capture_output=True)


def sandbox_from_environment() -> SandboxRunner:
    production = os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production"}
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
