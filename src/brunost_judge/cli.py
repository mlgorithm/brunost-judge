"""Small, dependency-free CLI for task authors and local judge runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from brunost_judge.task import SUPPORTED_KINDS, scaffold_task, validate_task
from grader.harness import run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunost", description="Brunost Judge task and local execution CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    task = subparsers.add_parser("task", help="create and validate task packages")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    new = task_sub.add_parser("new", help="scaffold a task package")
    new.add_argument("kind", choices=sorted(SUPPORTED_KINDS))
    new.add_argument("path", type=Path)
    new.add_argument("--force", action="store_true")
    validate = task_sub.add_parser("validate", help="validate a task package")
    validate.add_argument("path", type=Path)

    run_parser = subparsers.add_parser("run", help="run a task scorer locally")
    run_parser.add_argument("task", type=Path)
    run_parser.add_argument("--submission", required=True, type=Path)
    run_parser.add_argument("--result", type=Path, help="write canonical JSON result to this file")
    return parser


def _validate(path: Path) -> int:
    result = validate_task(path)
    if result.valid:
        print(f"valid task: {result.path} (kind={result.kind})")
        return 0
    print(f"invalid task: {result.path}", file=sys.stderr)
    for error in result.errors:
        print(f"  - {error}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "task" and args.task_command == "new":
        try:
            path = scaffold_task(args.path, args.kind, force=args.force)
        except (FileExistsError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"created task: {path}")
        return 0
    if args.command == "task" and args.task_command == "validate":
        return _validate(args.path)
    if args.command == "run":
        validation = validate_task(args.task)
        if not validation.valid:
            return _validate(args.task)
        result = run(str(args.submission), str(args.task))
        encoded = json.dumps(result, sort_keys=True, indent=2)
        if args.result:
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0 if result.get("status") == "completed" else 1
    return 2
