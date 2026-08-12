"""Small, dependency-free CLI for task authors and local judge runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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

    server = subparsers.add_parser("server", help="run the standalone HTTP API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", default=8787, type=int)
    server.add_argument("--database", type=Path)

    worker = subparsers.add_parser("worker", help="run a local execution worker")
    worker.add_argument("--database", type=Path)
    worker.add_argument("--poll-seconds", default=1.0, type=float)
    worker.add_argument("--once", action="store_true")
    init = subparsers.add_parser("init", help="create a local judge project")
    init.add_argument("path", nargs="?", type=Path, default=Path("."))
    up = subparsers.add_parser("up", help="start the Docker Compose reference deployment")
    up.add_argument("--detach", action="store_true")
    up.add_argument("--file", type=Path, default=Path("docker-compose.yml"))
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
    if args.command == "server":
        try:
            import uvicorn

            from brunost_judge.server import create_app
        except ImportError:
            print("Install brunost-judge[server] to run the API", file=sys.stderr)
            return 2
        os.environ["BRUNOST_JUDGE_IMPORT_APP"] = "false"
        uvicorn.run(create_app(args.database), host=args.host, port=args.port)
        return 0
    if args.command == "worker":
        from brunost_judge.store import JudgeStore
        from brunost_judge.worker import LocalWorker

        worker = LocalWorker(JudgeStore(args.database or os.environ.get("BRUNOST_JUDGE_DB", "judge.db")), poll_seconds=args.poll_seconds)
        if args.once:
            return 0 if worker.process_one() is not None else 1
        worker.run_forever()
        return 0
    if args.command == "init":
        root = args.path.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "tasks").mkdir(exist_ok=True)
        config = root / "brunost.yaml"
        if not config.exists():
            config.write_text("version: 1\nname: my-judge\ndatabase: judge.db\n", encoding="utf-8")
        env = root / ".env.example"
        if not env.exists():
            env.write_text("# Set this for a non-local deployment\nBRUNOST_JUDGE_API_TOKEN=replace-me\n", encoding="utf-8")
        print(f"initialized judge project: {root}")
        return 0
    if args.command == "up":
        command = ["docker", "compose", "-f", str(args.file), "up", "--build"]
        if args.detach:
            command.append("--detach")
        return subprocess.run(command, check=False).returncode
    return 2
