"""Sandbox entrypoint: the worker runs `python evaluate.py` inside the sealed container.

Reads the contract paths from the environment, runs the harness, and writes the
canonical result to RESULT_PATH. Works whether the grader is importable as a package
(`grader.harness`) or bundled flat alongside this file in the assets directory.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

try:
    from grader.harness import run
except ImportError:  # bundled flat in the task assets (harness.py next to this file)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from harness import run  # type: ignore[no-redef]


def main() -> int:
    submission_path = os.environ.get("SUBMISSION_PATH", "/workspace/submission")
    assets_path = os.environ.get("ASSETS_PATH", "/workspace/assets")
    result_path = os.environ.get("RESULT_PATH", "/workspace/output/results.json")

    result = run(submission_path, assets_path)

    out = pathlib.Path(result_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result))
    # Surface a one-line summary for the worker logs (no secrets — public score only).
    print(f"grader: status={result.get('status')} score={result.get('score')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
