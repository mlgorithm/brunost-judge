"""Run one task scorer in a separately limited process."""

from __future__ import annotations

import argparse
import contextlib
import json
import os

from grader.harness import _failed, _run_scorer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("submission_path")
    parser.add_argument("assets_path")
    parser.add_argument("--official-split")
    parser.add_argument("--require-official", action="store_true")
    args = parser.parse_args()

    try:
        # Task scorers are allowed to print diagnostics, but stdout is reserved
        # for the small machine-readable result sent back to the harness.
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = _run_scorer(
                args.submission_path,
                args.assets_path,
                official_split=args.official_split,
                require_official=args.require_official,
            )
    except Exception as exc:  # noqa: BLE001 - contain task scorer failures
        result = _failed(f"Scorer raised an exception — {type(exc).__name__}: {exc}")
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
