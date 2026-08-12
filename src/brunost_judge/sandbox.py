"""Execution boundaries for untrusted task scorers.

The process runner is convenient for local development. Official deployments
should select the Docker runner with a gVisor/Kata runtime. Docker arguments are
always passed as argv items and contestant/task mounts are read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
            output = Path(output_dir)
            output.chmod(0o777)
            label = f"brunost.judge.execution={execution_id}"
            command = [
                "docker", "run", "--rm", "--label", label,
                "--runtime", self.runtime, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", str(self.pids_limit), "--memory", self.memory,
                "--cpus", self.cpus, "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--user", "65534:65534",
                "--volume", f"{submission}:/workspace/submission:ro",
                "--volume", f"{task}:/workspace/assets:ro",
                "--volume", f"{output}:/workspace/output:rw",
                "--env", "SUBMISSION_PATH=/workspace/submission",
                "--env", "ASSETS_PATH=/workspace/assets",
                "--env", "RESULT_PATH=/workspace/output/results.json",
                "--env", "HOME=/tmp",
            ]
            if self.seccomp_profile:
                command.extend(["--security-opt", f"seccomp={self.seccomp_profile}"])
            command.extend([self.image, "python", "-m", "grader.evaluate"])
            try:
                completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=max(1, self.timeout_seconds))
            except subprocess.TimeoutExpired:
                self._cleanup(label)
                return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"sandbox timed out after {self.timeout_seconds}s"}
            result_path = output / "results.json"
            if result_path.is_file():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(result, dict):
                        if completed.returncode != 0 and result.get("status") == "completed":
                            result["status"] = "failed"
                            result["failure_reason"] = "sandbox exited unsuccessfully"
                        return result
                except (OSError, json.JSONDecodeError):
                    pass
            detail = (completed.stderr or completed.stdout or "sandbox produced no result").strip()
            return {"status": "failed", "score": 0.0, "metrics": {}, "failure_reason": f"sandbox exit {completed.returncode}: {detail[:1800]}"}

    @staticmethod
    def _cleanup(label: str) -> None:
        containers = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={label}"], check=False, capture_output=True, text=True).stdout.split()
        if containers:
            subprocess.run(["docker", "rm", "-f", *containers], check=False, capture_output=True)


def sandbox_from_environment() -> SandboxRunner:
    mode = os.environ.get("BRUNOST_JUDGE_SANDBOX_MODE", "process").strip().lower()
    if mode in {"process", "local", "development"}:
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
