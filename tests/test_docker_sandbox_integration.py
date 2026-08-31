import json
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


def test_docker_sandbox_drops_privileges_before_native_candidate_compilation(tmp_path: Path):
    """A C compiler must not be able to include a root-only private asset."""

    task = tmp_path / "task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: coding\nrunner: classic\nlanguage: c\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("1\n", encoding="utf-8")
    (task / "tests" / "one.ans").write_text("2\n", encoding="utf-8")
    (task / "private" / "compile-secret.h").write_text(
        '#define COMPILE_SECRET "PRIVATE_NATIVE_COMPILE_LEAK"\n', encoding="utf-8"
    )
    (submission / "solution.c").write_text(
        '#include "/tmp/brunost-assets/private/compile-secret.h"\n'
        "#include <stdio.h>\n"
        "int main(void) { puts(COMPILE_SECRET); return 0; }\n",
        encoding="utf-8",
    )
    seccomp = Path(__file__).parents[1] / "src" / "brunost_judge" / "security" / "seccomp-v1.json"

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
        seccomp_profile=str(seccomp),
    ).run(submission, task, "docker-native-compile-privacy")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(0.0), result
    assert result["metrics"]["verdict"] == "CE", result
    assert "PRIVATE_NATIVE_COMPILE_LEAK" not in str(result)


def test_docker_sandbox_compiles_a_native_candidate_after_the_uid_drop(tmp_path: Path):
    task = tmp_path / "task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: coding\nrunner: classic\nlanguage: c\n"
        "answer_source: reference\nreference_language: c\nreference_entrypoint: private/reference.c\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("21\n", encoding="utf-8")
    (task / "private" / "reference.c").write_text(
        "#include <stdio.h>\n"
        "int main(void) { int value; scanf(\"%d\", &value); printf(\"%d\\n\", value * 2); }\n",
        encoding="utf-8",
    )
    (submission / "solution.c").write_text(
        "#include <stdio.h>\n"
        "int main(void) { int value; scanf(\"%d\", &value); printf(\"%d\\n\", value * 2); }\n",
        encoding="utf-8",
    )
    seccomp = Path(__file__).parents[1] / "src" / "brunost_judge" / "security" / "seccomp-v1.json"

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
        seccomp_profile=str(seccomp),
    ).run(submission, task, "docker-native-compile-success")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result
    assert result["metrics"]["verdict"] == "AC", result


def test_docker_sandbox_judges_interactive_task(tmp_path: Path):
    task = tmp_path / "interactive-task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: interactive\nrunner: classic\nlanguage: c\n"
        "interactor: interactor.py\ntime_limit_ms: 1000\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("5\n", encoding="utf-8")
    (task / "private" / "runtime-secret.txt").write_text("not for contestants\n", encoding="utf-8")
    (task / "interactor.py").write_text(
        "def interact(session, input_path):\n"
        "    session.send('ready')\n"
        "    return int(session.receive()) == 10\n",
        encoding="utf-8",
    )
    (submission / "solution.c").write_text(
        "#include <stdio.h>\n"
        "#include <string.h>\n"
        "int main(void) { FILE *secret = fopen(\"/tmp/brunost-assets/private/runtime-secret.txt\", \"r\"); char line[64]; if (secret) { fclose(secret); puts(\"0\"); } else if (fgets(line, sizeof line, stdin) && strcmp(line, \"ready\\n\") == 0) { puts(\"10\"); } fflush(stdout); }\n",
        encoding="utf-8",
    )

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
    ).run(submission, task, "docker-interactive-integration")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result


def test_docker_sandbox_compiles_private_optimization_baseline_without_exposing_it(tmp_path: Path):
    task = tmp_path / "optimization-task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: optimization\nrunner: optimization\nlanguage: c\n"
        "evaluator_entrypoint: private/evaluator.py\nbaseline_enabled: true\n"
        "baseline_entrypoint: private/baseline.c\nobjective_direction: maximize\n"
        "score_mode: baseline_ratio\naggregation: mean\ntime_limit_ms: 1000\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("21\n", encoding="utf-8")
    (task / "private" / "evaluator.py").write_text(
        "def evaluate(input_path, output_path):\n"
        "    value = int(open(output_path, encoding='utf-8').read())\n"
        "    return {'feasible': value >= 0, 'objective': float(value)}\n",
        encoding="utf-8",
    )
    solution = (
        "#include <stdio.h>\n"
        "int main(void) { int value; scanf(\"%d\", &value); printf(\"%d\\n\", value * 2); }\n"
    )
    (task / "private" / "baseline.c").write_text(solution, encoding="utf-8")
    (submission / "solution.c").write_text(solution, encoding="utf-8")
    seccomp = Path(__file__).parents[1] / "src" / "brunost_judge" / "security" / "seccomp-v1.json"

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
        seccomp_profile=str(seccomp),
    ).run(submission, task, "docker-optimization-native-baseline")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result
    assert result["metrics"]["verdict"] == "OK", result


def test_docker_sandbox_runs_game_plugin_bundle(tmp_path: Path):
    task = tmp_path / "game-task"
    submission = tmp_path / "submission"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    submission.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: game\nrunner: python\n", encoding="utf-8")
    (task / "runner.py").write_text(
        "from pathlib import Path\n"
        "from grader.agent_runtime import AgentLimits, AgentRuntime\n"
        "def run(context):\n"
        "    assert Path(context['participants']['red']).is_dir()\n"
        "    runtime = AgentRuntime.from_context(context, limits=AgentLimits(turn_timeout_seconds=0.5, total_timeout_seconds=5))\n"
        "    with runtime:\n"
        "        actions = runtime.step({'round': 1})\n"
        "    assert actions == {0: {'seat': 0, 'round': 1}}\n"
        "    output = Path(context['output_path'])\n"
        "    output.mkdir(parents=True, exist_ok=True)\n"
        "    (output / 'replay.jsonl').write_text('{\\\"round\\\": 1}\\n', encoding='utf-8')\n"
        "    return {'status': 'completed', 'score': 1.0, 'scores': {'red': 1.0}, 'replay': {'seed': context['seed']}, 'artifacts': {'replay': {'path': 'replay.jsonl', 'kind': 'replay'}}}\n",
        encoding="utf-8",
    )
    (submission / "participants" / "agent-0").mkdir(parents=True)
    (submission / "participants" / "agent-0" / "agent.py").write_text(
        "import json\n"
        "import sys\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message['type'] == 'init':\n"
        "        print(json.dumps({'type': 'ready'}), flush=True)\n"
        "    elif message['type'] == 'turn':\n"
        "        print(json.dumps({'type': 'action', 'action': {'seat': message['seat'], 'round': message['state']['round']}}), flush=True)\n"
        "    elif message['type'] == 'shutdown':\n"
        "        break\n",
        encoding="utf-8",
    )
    (submission / ".brunost").mkdir()
    (submission / ".brunost" / "plugin.json").write_text(
        json.dumps(
            {
                "version": 1,
                "execution_id": "docker-plugin-integration",
                "task_ref": "game/v1",
                "evaluation_kind": "match",
                "participants": {"red": "participants/agent-0"},
                "seats": [{"agent_id": "red", "seat": 0, "path": "participants/agent-0"}],
                "seed": 19,
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    result = DockerSandboxRunner(
        os.environ["BRUNOST_JUDGE_SANDBOX_IMAGE"],
        os.environ.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runc"),
        timeout_seconds=30,
    ).run(submission, task, "docker-plugin-integration")

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result
    assert result["metrics"]["scores"] == {"red": 1.0}
    assert result["_artifact_payloads"]["replay"]["data"]
