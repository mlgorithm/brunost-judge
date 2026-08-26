import shutil

import pytest

from brunost_judge.contracts import ExecutionRequest, TaskRecord
from brunost_judge.store import JudgeStore
from brunost_judge.task import validate_task
from brunost_judge.worker import LocalWorker
from grader.classic import _sandbox_command, run_classic
from grader.harness import run


def _task(root):
    task = root / "task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests" / "basic").mkdir(parents=True)
    (task / "tests" / "full").mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: python\ntime_limit_ms: 1000\nscoring_mode: percentage\n",
        encoding="utf-8",
    )
    (task / "tests" / "basic" / "one.in").write_text("2\n", encoding="utf-8")
    (task / "tests" / "basic" / "one.ans").write_text("4\n", encoding="utf-8")
    (task / "tests" / "full" / "one.in").write_text("5\n", encoding="utf-8")
    (task / "tests" / "full" / "one.ans").write_text("10\n", encoding="utf-8")
    return task


def _interactive_task(root):
    task = root / "interactive-task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: interactive\nrunner: classic\nlanguage: python\n"
        "time_limit_ms: 1000\noutput_limit_bytes: 65536\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("5\n", encoding="utf-8")
    (task / "interactor.py").write_text(
        "def interact(session, input_path):\n"
        "    with open(input_path, encoding='utf-8') as source:\n"
        "        value = int(source.read())\n"
        "    session.send('ready')\n"
        "    answer = int(session.receive())\n"
        "    return {'ok': answer == value * 2, 'message': 'checked'}\n",
        encoding="utf-8",
    )
    return task


def _reference_task(root):
    task = root / "reference-task"
    (task / "public").mkdir(parents=True)
    (task / "private").mkdir()
    (task / "tests").mkdir()
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: python\n"
        "time_limit_ms: 1000\nanswer_source: reference\n"
        "reference_language: python\nreference_entrypoint: private/reference.py\n",
        encoding="utf-8",
    )
    (task / "tests" / "one.in").write_text("5\n", encoding="utf-8")
    (task / "private" / "reference.py").write_text(
        "import sys\nprint(int(sys.stdin.read()) * 2)\n", encoding="utf-8"
    )
    return task


def test_classic_python_runner_dispatches_and_awards_percentage_score(tmp_path):
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(
        "import sys\nprint(int(sys.stdin.read()) * 2)\n", encoding="utf-8"
    )

    result = run(str(submission), str(task))

    assert result["status"] == "completed"
    assert result["score"] == pytest.approx(1.0)
    assert result["metrics"]["runner"] == "classic"
    assert result["metrics"]["verdict"] == "AC"
    assert result["metrics"]["scoring_mode"] == "percentage"
    assert result["metrics"]["passed_tests"] == 2
    assert result["metrics"]["total_tests"] == 2


def test_classic_reference_runner_generates_answers_inside_the_judge(tmp_path):
    task = _reference_task(tmp_path)
    validation = validate_task(task)
    assert validation.valid, validation.errors
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(
        "import sys\nprint(int(sys.stdin.read()) * 2)\n", encoding="utf-8"
    )

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0)
    assert result["metrics"]["verdict"] == "AC"


def test_classic_reference_runner_rejects_wrong_submission_without_answer_files(tmp_path):
    task = _reference_task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text("print(0)\n", encoding="utf-8")

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(0.0)
    assert result["metrics"]["verdict"] == "WA"


def test_classic_runner_awards_percentage_for_solved_tests(tmp_path):
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(
        "import sys\nvalue = int(sys.stdin.read())\nprint(4 if value == 2 else 0)\n",
        encoding="utf-8",
    )

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed"
    assert result["score"] == pytest.approx(0.5)
    assert result["metrics"]["verdict"] == "WA"
    assert result["metrics"]["passed_tests"] == 1


def test_classic_runner_keeps_whole_task_scoring(tmp_path):
    task = _task(tmp_path)
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: python\nscoring_mode: all_or_nothing\n",
        encoding="utf-8",
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text("print(4)\n", encoding="utf-8")

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed"
    assert result["score"] == pytest.approx(0.0)
    assert result["metrics"]["scoring_mode"] == "all_or_nothing"


def test_classic_runner_uses_custom_checker(tmp_path):
    task = _task(tmp_path)
    (task / "checker.py").write_text(
        "def check(input_path, answer_path, output_path):\n"
        "    with open(output_path, encoding='utf-8') as output:\n"
        "        return {'ok': output.read().strip().split()[0] == '4'}\n",
        encoding="utf-8",
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text("print('4 extra tokens')\n", encoding="utf-8")

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed"
    assert result["score"] == pytest.approx(1.0)
    assert result["metrics"]["verdict"] == "AC"


def test_local_worker_executes_registered_classic_task(tmp_path):
    task = _task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(
        "import sys\nprint(int(sys.stdin.read()) * 2)\n", encoding="utf-8"
    )
    store = JudgeStore(tmp_path / "judge.db")
    store.register_task(TaskRecord("classic/v1", str(task), "icpc"))
    store.submit(ExecutionRequest("classic/v1", str(submission), "classic-attempt"))

    result = LocalWorker(store).process_one()

    assert result is not None
    assert result.status == "completed"
    assert result.score == pytest.approx(1.0)


def test_classic_runner_reports_time_limit(tmp_path):
    task = _task(tmp_path)
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: python\ntime_limit_ms: 100\n",
        encoding="utf-8",
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text("while True: pass\n", encoding="utf-8")

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed"
    assert result["score"] == 0.0
    assert result["metrics"]["verdict"] == "TLE"
    assert result["metrics"]["tests"][0]["verdict"] == "TLE"


def test_classic_runner_enforces_output_limit_after_clean_exit(tmp_path):
    task = _task(tmp_path)
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: python\n"
        "output_limit_bytes: 128\n",
        encoding="utf-8",
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text("print('x' * 10000)\n", encoding="utf-8")

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed", result
    assert result["score"] == 0.0
    assert result["metrics"]["verdict"] == "OLE"
    assert result["metrics"]["tests"][0]["verdict"] == "OLE"


def test_interactive_runner_dispatches_line_protocol(tmp_path):
    task = _interactive_task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(
        "import sys\n"
        "if input().strip() == 'ready':\n"
        "    print(10, flush=True)\n",
        encoding="utf-8",
    )

    result = run(str(submission), str(task))

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result
    assert result["metrics"]["runner"] == "interactive"
    assert result["metrics"]["verdict"] == "AC"
    assert result["metrics"]["tests"][0]["transcript"] == [
        {"direction": "judge", "value": "ready"},
        {"direction": "candidate", "value": "10"},
    ]


def test_interactive_runner_reports_interactor_verdict(tmp_path):
    task = _interactive_task(tmp_path)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "solution.py").write_text(
        "input()\nprint(11, flush=True)\n",
        encoding="utf-8",
    )

    result = run(str(submission), str(task))

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(0.0), result
    assert result["metrics"]["verdict"] == "WA"


def test_interactive_task_validation_requires_interactor_and_tests(tmp_path):
    task = _interactive_task(tmp_path)
    (task / "interactor.py").unlink()
    (task / "tests" / "one.in").unlink()

    result = validate_task(task)

    assert not result.valid
    assert "interactive tasks need interactor.py" in result.errors
    assert "interactive tasks need tests/*.in files" in result.errors


def test_classic_task_validation_requires_test_answers(tmp_path):
    task = _task(tmp_path)
    (task / "tests" / "full" / "one.ans").unlink()

    result = validate_task(task)

    assert not result.valid
    assert any("missing .ans or .out" in error for error in result.errors)


def test_classic_task_validation_requires_supported_manifest_version(tmp_path):
    task = _task(tmp_path)
    (task / "judge.yaml").write_text(
        "version: 2\nkind: icpc\nrunner: classic\nlanguage: python\n",
        encoding="utf-8",
    )

    result = validate_task(task)

    assert not result.valid
    assert any("unsupported judge.yaml version" in error for error in result.errors)


def test_production_classic_command_gets_a_private_mount_namespace(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setenv("BRUNOST_JUDGE_CLASSIC_USE_BWRAP", "true")
    monkeypatch.setattr("grader.classic.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    command = _sandbox_command(["/usr/bin/python", str(build / "solution.py")], build)

    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in command
    assert "/work/solution.py" in command
    assert "/workspace/assets" not in command


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ is not installed")
def test_classic_cpp_submission_compiles_and_runs(tmp_path):
    task = _task(tmp_path)
    (task / "judge.yaml").write_text(
        "version: 1\nkind: icpc\nrunner: classic\nlanguage: cpp\n",
        encoding="utf-8",
    )
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "main.cpp").write_text(
        "#include <iostream>\nint main(){long long n; std::cin>>n; std::cout<<n*2<<'\\n';}\n",
        encoding="utf-8",
    )

    result = run_classic(str(submission), str(task))

    assert result["status"] == "completed", result
    assert result["score"] == pytest.approx(1.0), result
    assert result["metrics"]["language"] == "cpp"
