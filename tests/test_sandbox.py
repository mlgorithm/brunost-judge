import subprocess
from pathlib import Path

import pytest

from brunost_judge.sandbox import (
    DockerSandboxRunner,
    ProcessSandboxRunner,
    sandbox_from_environment,
)


def test_process_runner_is_available_for_local_development(tmp_path: Path):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    (task / "scorer").mkdir(parents=True)
    (task / "public").mkdir()
    (task / "private").mkdir()
    submission.mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 0.5\n", encoding="utf-8")
    assert ProcessSandboxRunner().run(submission, task, "local")["score"] == 0.5


def test_docker_runner_uses_hardened_flags(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    task.mkdir()
    submission.mkdir()
    monkeypatch.setattr("brunost_judge.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    def fake_run(command, **kwargs):
        assert "--runtime" in command and command[command.index("--runtime") + 1] == "runsc"
        assert "--network" in command and command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
        assert "--pids-limit" in command
        volume_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--volume"]
        output_mount = next(value for value in volume_args if value.endswith(":/workspace/output:rw"))
        output_path = Path(output_mount.rsplit(":/workspace/output:rw", 1)[0])
        output_path.joinpath("results.json").write_text('{"status":"completed","score":1}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", fake_run)
    result = DockerSandboxRunner("judge@sha256:" + "a" * 64, "runsc").run(submission, task, "exec-1")
    assert result["status"] == "completed"


def test_docker_mode_requires_image_and_runtime(monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_MODE", "docker")
    monkeypatch.delenv("BRUNOST_JUDGE_SANDBOX_IMAGE", raising=False)
    with pytest.raises(RuntimeError, match="SANDBOX_IMAGE"):
        sandbox_from_environment()


def test_docker_mode_requires_seccomp(monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_MODE", "docker")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGE", "judge@sha256:" + "a" * 64)
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runsc")
    monkeypatch.delenv("BRUNOST_JUDGE_SANDBOX_SECCOMP", raising=False)
    with pytest.raises(RuntimeError, match="SANDBOX_SECCOMP"):
        sandbox_from_environment()
