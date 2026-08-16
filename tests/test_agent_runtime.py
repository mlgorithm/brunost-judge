import sys
from pathlib import Path

import pytest

from brunost_judge.agent_runtime import (
    AgentCrashed,
    AgentLimits,
    AgentProtocolError,
    AgentRuntime,
    AgentSpec,
    AgentTimeout,
)


def _agent(root: Path, body: str) -> Path:
    root.mkdir()
    (root / "agent.py").write_text(body, encoding="utf-8")
    return root


READY_AGENT = """import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message[\"type\"] == \"init\":
        print(json.dumps({\"type\": \"ready\"}), flush=True)
    elif message[\"type\"] == \"turn\":
        print(json.dumps({\"type\": \"action\", \"action\": {\"seat\": message[\"seat\"] if \"seat\" in message else None, \"turn\": message[\"turn\"]}}), flush=True)
    elif message[\"type\"] == \"shutdown\":
        break
"""


def test_runtime_launches_one_process_per_seat_in_order(tmp_path: Path):
    artifact = _agent(tmp_path / "agent", READY_AGENT)
    runtime = AgentRuntime.from_context(
        {
            "participants": {"red": str(artifact), "blue": str(artifact)},
            "seats": [
                {"agent_id": "blue", "seat": 1},
                {"agent_id": "red", "seat": 0},
            ],
            "seed": 17,
        },
        limits=AgentLimits(startup_timeout_seconds=2, turn_timeout_seconds=1, total_timeout_seconds=5),
    )
    with runtime:
        actions = runtime.step({"round": 1})

    assert actions == {0: {"seat": 0, "turn": 1}, 1: {"seat": 1, "turn": 1}}
    assert [spec.seat for spec in runtime.specs] == [0, 1]


def test_runtime_enforces_turn_timeout(tmp_path: Path):
    artifact = _agent(
        tmp_path / "slow-agent",
        """import json
import sys
import time

for line in sys.stdin:
    message = json.loads(line)
    if message[\"type\"] == \"init\":
        print(json.dumps({\"type\": \"ready\"}), flush=True)
    elif message[\"type\"] == \"turn\":
        time.sleep(1)
""",
    )
    runtime = AgentRuntime(
        (AgentSpec("slow", 0, str(artifact)),),
        limits=AgentLimits(startup_timeout_seconds=1, turn_timeout_seconds=0.05, total_timeout_seconds=2),
    )
    with pytest.raises(AgentTimeout, match="response timeout"), runtime:
        runtime.step({})


def test_runtime_contains_crashes_and_oversized_messages(tmp_path: Path):
    crashed = _agent(
        tmp_path / "crashed-agent",
        """import json
import sys
for line in sys.stdin:
    if json.loads(line)[\"type\"] == \"init\":
        print(json.dumps({\"type\": \"ready\"}), flush=True)
        raise SystemExit(3)
""",
    )
    with pytest.raises(AgentCrashed), AgentRuntime(
        (AgentSpec("crashed", 0, str(crashed)),),
        limits=AgentLimits(startup_timeout_seconds=1, turn_timeout_seconds=0.2, total_timeout_seconds=2),
    ) as runtime:
        runtime.step({})

    oversized = _agent(
        tmp_path / "oversized-agent",
        """import json
import sys
for line in sys.stdin:
    if json.loads(line)[\"type\"] == \"init\":
        print(json.dumps({\"type\": \"ready\"}), flush=True)
    elif json.loads(line)[\"type\"] == \"turn\":
        print(\"x\" * 100, flush=True)
""",
    )
    with pytest.raises(AgentProtocolError, match="exceeds size limit"), AgentRuntime(
        (AgentSpec("oversized", 0, str(oversized)),),
        limits=AgentLimits(startup_timeout_seconds=1, turn_timeout_seconds=1, total_timeout_seconds=2, max_message_bytes=32),
    ) as runtime:
        runtime.step({})


def test_runtime_uses_explicit_python_command(tmp_path: Path):
    artifact = _agent(tmp_path / "explicit", READY_AGENT)
    runtime = AgentRuntime(
        (AgentSpec("explicit", 0, str(artifact), command=(sys.executable, "-u", "agent.py")),),
        limits=AgentLimits(total_timeout_seconds=2),
    )
    with runtime:
        assert runtime.step({})[0]["turn"] == 1
