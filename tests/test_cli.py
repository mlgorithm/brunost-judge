from pathlib import Path

from brunost_judge.cli import main
from brunost_judge.deployment import render_country_bundle
from brunost_judge.task import validate_task


def test_task_scaffold_and_validation(tmp_path: Path, capsys):
    task = tmp_path / "task"
    assert main(["task", "new", "ioai", str(task)]) == 0
    assert main(["task", "validate", str(task)]) == 0
    assert "valid task" in capsys.readouterr().out


def test_model_task_scaffold_uses_v2_contract(tmp_path: Path):
    task = tmp_path / "model-task"
    assert main(["task", "new", "model", str(task)]) == 0
    assert main(["task", "validate", str(task)]) == 0
    manifest = (task / "judge.yaml").read_text(encoding="utf-8")
    assert "version: 2" in manifest
    assert "model_contract: train_predict_v2" in manifest
    assert (task / "evaluator.py").is_file()
    assert not (task / "scorer" / "metrics.py").exists()


def test_task_validation_reports_missing_files(tmp_path: Path, capsys):
    task = tmp_path / "task"
    task.mkdir()
    assert main(["task", "validate", str(task)]) == 2
    assert "missing judge.yaml" in capsys.readouterr().err


def test_ioai_validation_checks_the_declared_scorer_contract(tmp_path: Path):
    task = tmp_path / "task"
    for directory in ("public", "private", "scorer"):
        (task / directory).mkdir(parents=True, exist_ok=True)
    (task / "judge.yaml").write_text(
        "version: 1\nkind: ioai\nscoring: other.module:evaluate\nnetwork: enabled\nfeedback: validation\n",
        encoding="utf-8",
    )
    (task / "scorer" / "metrics.py").write_text("def wrong_name(): pass\n", encoding="utf-8")

    validation = validate_task(task)

    assert not validation.valid
    assert "scoring must be scorer.metrics:evaluate" in validation.errors
    assert "scorer must define evaluate()" in validation.errors
    assert "generic scorer tasks must declare network: disabled" in validation.errors
    assert "feedback is platform policy; do not declare it in a Judge task manifest" in validation.errors


def test_cluster_init_generates_private_operator_environment(tmp_path: Path, capsys):
    assert main(["cluster", "init", str(tmp_path), "--domain", "judge.example.test"]) == 0
    env = tmp_path / ".env"
    assert env.exists()
    contents = env.read_text(encoding="utf-8")
    assert "BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true" in contents
    assert "BRUNOST_JUDGE_DOMAIN=judge.example.test" in contents
    assert (tmp_path / "brunost-cluster.json").exists()
    assert (tmp_path / "docker-compose.control.yml").exists()
    assert (tmp_path / "docker-compose.worker.yml").exists()
    assert (tmp_path / "RUNBOOK.md").exists()
    assert "created cluster configuration" in capsys.readouterr().out


def test_generated_bundle_respects_container_entrypoints(tmp_path: Path):
    render_country_bundle(tmp_path, force=True)
    control = (tmp_path / "docker-compose.control.yml").read_text(encoding="utf-8")
    worker = (tmp_path / "docker-compose.worker.yml").read_text(encoding="utf-8")
    assert 'command: ["server"' in control
    assert 'command: ["worker"' in worker
    assert 'command: ["brunost"' not in control + worker


def test_canary_uses_immutable_artifacts_and_checks_idempotency(tmp_path: Path, monkeypatch, capsys):
    task = tmp_path / "task"
    task.mkdir()
    (task / "judge.yaml").write_text("version: 1\nkind: ioai\n", encoding="utf-8")
    (task / "public").mkdir()
    (task / "private").mkdir()
    (task / "scorer").mkdir()
    (task / "scorer" / "metrics.py").write_text("def evaluate(s, a): return 1.0\n", encoding="utf-8")
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "answer.txt").write_text("ok\n", encoding="utf-8")

    submitted: list[dict] = []

    class FakeClient:

        def __init__(self, *_args, **_kwargs):
            self.submitted = submitted

        def upload_artifact(self, path):
            return {"artifact_id": "a" * 64, "path": str(path)}

        def register_task(self, **kwargs):
            assert "artifact_id" in kwargs
            assert "path" not in kwargs
            return {"manifest": {"digest": "b" * 64}}

        def submit(self, **kwargs):
            assert "submission_artifact_id" in kwargs
            assert "submission_path" not in kwargs
            self.submitted.append(kwargs)
            return {"execution_id": "canary-execution", "result_version": 1}

        def get_execution(self, _execution_id):
            return {"execution_id": "canary-execution", "status": "completed", "score": 1.0}

    monkeypatch.setattr("brunost_judge.cli.JudgeClient", FakeClient)
    assert main(["canary", "--task-path", str(task), "--submission", str(submission)]) == 0
    assert len(submitted) == 2
    output = capsys.readouterr().out
    assert '"immutable_task_artifact": true' in output
    assert '"idempotency": true' in output
