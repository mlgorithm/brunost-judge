import os
import shutil
from pathlib import Path

import pytest

from brunost_judge.sandbox import DockerSandboxRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("BRUNOST_JUDGE_RUN_DOCKER_TESTS", "false").lower() != "true"
    or shutil.which("docker") is None,
    reason="set BRUNOST_JUDGE_RUN_DOCKER_TESTS=true with Docker to run the sandbox integration test",
)


def test_docker_sandbox_judges_classic_task_without_task_visibility(tmp_path: Path):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: python\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("1\n", encoding="utf-8")
    (task / "tests" / "one.ans").write_text("2\n", encoding="utf-8")
    (submission / "solution.py").write_text(
        "try:\n"
        "    for path in ('/workspace/assets/tests/one.ans', '/tmp/brunost-assets/tests/one.ans'):\n"
        "        open(path, encoding='utf-8').read()\n"
        "except OSError:\n"
        "    print(int(input()) * 2)\n"
        "else:\n"
        "    print('PRIVATE_DATA_LEAK')\n",
        encoding="utf-8",
    )

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
    ).run(submission, task, "docker-integration")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result


def test_docker_sandbox_judges_interactive_task(tmp_path: Path):
    task = tmp_path / "interactive-task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: interactive\nrunner: classic\nlanguage: python\n"
        "interactor: interactor.py\ntime_limit_ms: 1000\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("5\n", encoding="utf-8")
    (task / "interactor.py").write_text(
        "def interact(session, input_path):\n"
        "    session.send('ready')\n"
        "    return int(session.receive()) == 10\n",
        encoding="utf-8",
    )
    (submission / "solution.py").write_text(
        "if input().strip() == 'ready':\n    print(10, flush=True)\n",
        encoding="utf-8",
    )

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
    ).run(submission, task, "docker-interactive-integration")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result
