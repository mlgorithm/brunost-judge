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
