import json
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
        assert "--interactive" in command
        assert "--read-only" in command
        assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
        assert "--pids-limit" in command
        assert "--init" in command
        assert "--ulimit" in command
        assert "/workspace/work:rw,exec,nosuid,nodev,size=256m" in command
        assert "BRUNOST_JUDGE_TASK_BUNDLE=stdin" in command
        assert "BRUNOST_JUDGE_WORK_ROOT=/workspace/work" in command
        assert not any("/workspace/assets" in value for value in command)
        volume_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--volume"]
        output_mount = next(value for value in volume_args if value.endswith(":/workspace/output:rw"))
        output_path = Path(output_mount.rsplit(":/workspace/output:rw", 1)[0])
        output_path.joinpath("results.json").write_text('{"status":"completed","score":1}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", fake_run)
    result = DockerSandboxRunner("judge@sha256:" + "a" * 64, "runsc").run(submission, task, "exec-1")
    assert result["status"] == "completed"


def test_bundled_seccomp_profile_is_versioned_and_fails_closed():
    profile_path = Path(__file__).parents[1] / "src" / "brunost_judge" / "security" / "seccomp-v1.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["defaultErrnoRet"] == 1
    assert any(
        entry["action"] == "SCMP_ACT_ERRNO" and "clone3" in entry["names"]
        for entry in profile["syscalls"]
    )
    privileged = next(entry for entry in profile["syscalls"] if "mount" in entry["names"])
    assert privileged["includes"]["caps"] == ["CAP_SYS_ADMIN"]


def test_production_compose_uses_the_bundled_versioned_seccomp_profile():
    compose = (Path(__file__).parents[1] / "docker-compose.production.yml").read_text(encoding="utf-8")

    assert "BRUNOST_JUDGE_SANDBOX_SECCOMP: /etc/docker/seccomp/brunost-seccomp-v1.json" in compose
    assert "./src/brunost_judge/security/seccomp-v1.json" in compose


def test_docker_runner_passes_model_evaluation_profile(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    task.mkdir()
    submission.mkdir()
    monkeypatch.setenv("BRUNOST_EVALUATION_PROFILE", "post_competition")
    monkeypatch.setattr("brunost_judge.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    def fake_run(command, **kwargs):
        assert "BRUNOST_EVALUATION_PROFILE=post_competition" in command
        volume_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--volume"]
        output_mount = next(value for value in volume_args if value.endswith(":/workspace/output:rw"))
        Path(output_mount.rsplit(":/workspace/output:rw", 1)[0]).joinpath("results.json").write_text(
            '{"status":"completed","score":1}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", fake_run)
    result = DockerSandboxRunner("judge@sha256:" + "a" * 64, "runsc").run(submission, task, "exec-profile")
    assert result["status"] == "completed"


def test_docker_runner_selects_image_from_task_runtime(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    task.mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text("kind: model\nruntime: python-3.13-ml-v1\n", encoding="utf-8")
    monkeypatch.setattr("brunost_judge.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    selected: list[str] = []

    def fake_run(command, **kwargs):
        selected.append(command[-4])
        volume_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--volume"]
        output_mount = next(value for value in volume_args if value.endswith(":/workspace/output:rw"))
        Path(output_mount.rsplit(":/workspace/output:rw", 1)[0]).joinpath("results.json").write_text(
            '{"status":"completed","score":1}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", fake_run)
    result = DockerSandboxRunner(
        "judge@sha256:" + "a" * 64,
        "runsc",
        runtime_images={"python-3.13-ml-v1": "ml@sha256:" + "b" * 64},
    ).run(submission, task, "exec-runtime")
    assert result["status"] == "completed"
    assert selected == ["ml@sha256:" + "b" * 64]


def test_docker_runner_rejects_invalid_result_payload(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    task.mkdir()
    submission.mkdir()
    monkeypatch.setattr("brunost_judge.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    def fake_run(command, **kwargs):
        volume_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--volume"]
        output_mount = next(value for value in volume_args if value.endswith(":/workspace/output:rw"))
        Path(output_mount.rsplit(":/workspace/output:rw", 1)[0]).joinpath("results.json").write_text(
            '{"status":"completed","score":"invalid","metrics":{}}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", fake_run)
    result = DockerSandboxRunner("judge@sha256:" + "a" * 64, "runsc").run(submission, task, "exec-2")

    assert result["status"] == "failed"
    assert "invalid sandbox result" in result["failure_reason"]


def test_docker_runner_rejects_an_oversized_result_file(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    task.mkdir()
    submission.mkdir()
    monkeypatch.setattr("brunost_judge.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    def fake_run(command, **kwargs):
        volume_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--volume"]
        output_mount = next(value for value in volume_args if value.endswith(":/workspace/output:rw"))
        Path(output_mount.rsplit(":/workspace/output:rw", 1)[0]).joinpath("results.json").write_text(
            '{"status":"completed","score":1,"metrics":{"trace":"' + "x" * 1_000_000 + '"}}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", fake_run)
    result = DockerSandboxRunner("judge@sha256:" + "a" * 64, "runsc").run(submission, task, "exec-oversized")

    assert result["status"] == "failed"
    assert "exceeds the output limit" in result["failure_reason"]


def test_docker_runner_contains_docker_start_failures(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    task.mkdir()
    submission.mkdir()
    monkeypatch.setattr("brunost_judge.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("brunost_judge.sandbox.subprocess.run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("daemon unavailable")))

    result = DockerSandboxRunner("judge@sha256:" + "a" * 64, "runsc").run(submission, task, "exec-start-failure")

    assert result["status"] == "failed"
    assert "could not start" in result["failure_reason"]


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


def test_production_mode_cannot_fall_back_to_process(monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ENV", "production")
    monkeypatch.delenv("BRUNOST_JUDGE_SANDBOX_MODE", raising=False)
    with pytest.raises(RuntimeError, match="explicit BRUNOST_JUDGE_SANDBOX_MODE"):
        sandbox_from_environment()

    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_MODE", "process")
    with pytest.raises(RuntimeError, match="in-process sandbox"):
        sandbox_from_environment()


def test_production_docker_mode_requires_digest_pinned_image(monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ENV", "production")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_MODE", "docker")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGE", "judge:latest")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runsc")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_SECCOMP", "/tmp/seccomp.json")
    with pytest.raises(RuntimeError, match="pinned by a sha256 digest"):
        sandbox_from_environment()


def test_production_docker_mode_validates_runtime_image_map(monkeypatch):
    monkeypatch.setenv("BRUNOST_JUDGE_ENV", "production")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_MODE", "docker")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGE", "judge@sha256:" + "a" * 64)
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runsc")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_SECCOMP", "/tmp/seccomp.json")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGES", '{"python-3.13-ml-v1":"ml:latest"}')
    with pytest.raises(RuntimeError, match="all production sandbox images"):
        sandbox_from_environment()

    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGES", '{"python-3.13-ml-v1":"ml@sha256:' + "b" * 64 + '"}')
    runner = sandbox_from_environment()
    assert isinstance(runner, DockerSandboxRunner)
    assert runner.runtime_images == {"python-3.13-ml-v1": "ml@sha256:" + "b" * 64}
