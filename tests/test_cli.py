from pathlib import Path

from brunost_judge.cli import main


def test_task_scaffold_and_validation(tmp_path: Path, capsys):
    task = tmp_path / "task"
    assert main(["task", "new", "ioai", str(task)]) == 0
    assert main(["task", "validate", str(task)]) == 0
    assert "valid task" in capsys.readouterr().out


def test_task_validation_reports_missing_files(tmp_path: Path, capsys):
    task = tmp_path / "task"
    task.mkdir()
    assert main(["task", "validate", str(task)]) == 2
    assert "missing judge.yaml" in capsys.readouterr().err


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
