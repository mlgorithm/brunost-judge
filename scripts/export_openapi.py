"""Export the standalone API schema for SDK and integration testing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brunost_judge.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, nargs="?", default=Path("openapi.json"))
    args = parser.parse_args()
    schema = create_app(":memory:").openapi()
    args.output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
