import json
import runpy
from pathlib import Path

from brunost_judge.cli import main

ROOT = Path(__file__).parents[1]


def test_reference_agents_pass_cli_smoke_validation(capsys):
    assert main(["agent", "validate", str(ROOT / "examples/agents/steady"), "--smoke"]) == 0
    assert "init/ready passed" in capsys.readouterr().out


def test_reference_game_runs_with_bundled_agents(tmp_path: Path):
    runner = runpy.run_path(str(ROOT / "examples/games/closest-number/runner.py"))["run"]
    context = {
        "participants": {
            "steady": str(ROOT / "examples/agents/steady"),
            "round-robin": str(ROOT / "examples/agents/round-robin"),
        },
        "seats": [
            {"agent_id": "steady", "seat": 0},
            {"agent_id": "round-robin", "seat": 1},
        ],
        "seed": 19,
        "output_path": str(tmp_path / "artifacts"),
    }

    result = runner(context)

    assert result["status"] == "completed"
    assert set(result["scores"]) == {"steady", "round-robin"}
    assert result["metrics"]["runtime"]["turns"] == 3
    assert (tmp_path / "artifacts" / "replay.jsonl").is_file()


def test_match_cli_runs_reference_game_and_writes_result(tmp_path: Path, capsys):
    output = tmp_path / "match-output"
    result_file = tmp_path / "result.json"

    assert main(
        [
            "match",
            "run",
            str(ROOT / "examples/games/closest-number"),
            "--agent",
            f"steady={ROOT / 'examples/agents/steady'}",
            "--agent",
            f"round-robin={ROOT / 'examples/agents/round-robin'}",
            "--seed",
            "19",
            "--output",
            str(output),
            "--result",
            str(result_file),
        ]
    ) == 0

    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["metrics"]["local_match"]["seed"] == 19
    assert (output / "replay.jsonl").is_file()
    assert '"status": "completed"' in capsys.readouterr().out
