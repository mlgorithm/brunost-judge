from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_public_contract_check_passes():
    completed = subprocess.run(
        [sys.executable, "scripts/check_public_contract.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "public contract check passed" in completed.stdout
