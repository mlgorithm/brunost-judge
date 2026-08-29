"""Small, dependency-free CLI for task authors and local judge runs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from brunost_judge.agent_protocol import protocol_spec
from brunost_judge.agent_runtime import (
    AgentLimits,
    AgentRuntime,
    AgentSpec,
    resolve_agent_command,
)
from brunost_judge.artifacts import artifact_id as digest_artifact
from brunost_judge.artifacts import pack_directory
from brunost_judge.auth import write_secret_file
from brunost_judge.deployment import render_country_bundle
from brunost_judge.enrollment import new_secret
from brunost_judge.games import AgentSeat
from brunost_judge.local_match import run_local_match
from brunost_judge.sdk import JudgeClient
from brunost_judge.task import (
    SUPPORTED_KINDS,
    scaffold_task,
    task_digest,
    validate_task,
)
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
    digest = task_sub.add_parser("digest", help="compute an immutable task package digest")
    digest.add_argument("path", type=Path)

    run_parser = subparsers.add_parser("run", help="run a task scorer locally")
    run_parser.add_argument("task", type=Path)
    run_parser.add_argument("--submission", required=True, type=Path)
    run_parser.add_argument("--result", type=Path, help="write canonical JSON result to this file")

    agent = subparsers.add_parser("agent", help="inspect and validate agent bundles")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_validate = agent_sub.add_parser("validate", help="validate an agent bundle and entrypoint")
    agent_validate.add_argument("path", type=Path)
    agent_validate.add_argument("--smoke", action="store_true", help="launch the agent and verify init/ready")
    agent_validate.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable output")
    agent_protocol = agent_sub.add_parser("protocol", help="print the versioned agent protocol summary")
    agent_protocol.add_argument("--json", action="store_true", dest="as_json", help="print compact JSON")

    match = subparsers.add_parser("match", help="run a local game match")
    match_sub = match.add_subparsers(dest="match_command", required=True)
    match_run = match_sub.add_parser("run", help="run a Python game runner with local agent bundles")
    match_run.add_argument("task", type=Path)
    match_run.add_argument("--agent", action="append", required=True, metavar="ID=PATH", help="agent bundle (repeatable)")
    match_run.add_argument("--seed", type=int, default=0)
    match_run.add_argument("--match-id", default="local-match")
    match_run.add_argument("--output", type=Path, default=Path("match-output"))
    match_run.add_argument("--result", type=Path, help="write canonical JSON result to this file")

    artifact = subparsers.add_parser("artifact", help="package and upload portable task/submission bundles")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    pack = artifact_sub.add_parser("pack", help="create a content-addressed tar.gz bundle")
    pack.add_argument("path", type=Path)
    pack.add_argument("--output", type=Path)
    upload = artifact_sub.add_parser("upload", help="upload a directory bundle to a judge API")
    upload.add_argument("path", type=Path)
    upload.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    upload.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))

    server = subparsers.add_parser("server", help="run the standalone HTTP API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", default=8787, type=int)
    server.add_argument("--database", type=str)

    auth = subparsers.add_parser("auth", help="manage judge service credentials")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    rotate_admin = auth_sub.add_parser("rotate-admin-token", help="atomically create or replace a private admin token file")
    rotate_admin.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("BRUNOST_JUDGE_API_TOKEN_FILE", ".brunost/admin-token")),
    )
    rotate_admin.add_argument("--force", action="store_true")

    worker = subparsers.add_parser("worker", help="run a local execution worker")
    worker.add_argument("--database", type=str)
    worker.add_argument("--poll-seconds", default=1.0, type=float)
    worker.add_argument("--worker-id")
    worker.add_argument("--queue", action="append", dest="queues", help="queue to consume (repeatable)")
    worker.add_argument("--resource-class", action="append", dest="resource_classes", help="resource class to consume (repeatable)")
    worker.add_argument("--capability", action="append", dest="capabilities", help="capability to advertise (repeatable)")
    worker.add_argument("--region", help="region to advertise")
    worker.add_argument("--lease-seconds", default=int(os.environ.get("BRUNOST_JUDGE_LEASE_SECONDS", "300")), type=int)
    worker.add_argument("--config", type=Path, help="enrolled node JSON config; use remote HTTPS worker mode")
    worker.add_argument("--path-map", action="append", default=[], metavar="REMOTE=LOCAL", help="map control-plane paths to local paths")
    worker.add_argument("--once", action="store_true")

    cluster = subparsers.add_parser("cluster", help="bootstrap and operate a country cluster")
    cluster_sub = cluster.add_subparsers(dest="cluster_command", required=True)
    cluster_init = cluster_sub.add_parser("init", help="create a generated cluster environment")
    cluster_init.add_argument("path", nargs="?", type=Path, default=Path("."))
    cluster_init.add_argument("--name", default="country-judge")
    cluster_init.add_argument("--domain", default="judge.example.org")
    cluster_init.add_argument("--cluster-id")
    cluster_init.add_argument("--force", action="store_true")
    issue = cluster_sub.add_parser("issue-node-token", help="create a short-lived one-time node join token")
    issue.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    issue.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))
    issue.add_argument("--node-id", required=True)
    issue.add_argument("--worker-id")
    issue.add_argument("--role", default="worker")
    issue.add_argument("--capability", action="append", default=[])
    issue.add_argument("--queue", action="append", default=[])
    issue.add_argument("--resource-class", action="append", dest="resource_classes", default=[])
    issue.add_argument("--region")
    issue.add_argument("--ttl-seconds", default=900, type=int)

    node = subparsers.add_parser("node", help="join and diagnose a remote worker node")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    join = node_sub.add_parser("join", help="enroll this node with a judge control plane")
    join.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    join.add_argument("--join-token", default=os.environ.get("BRUNOST_JUDGE_JOIN_TOKEN"))
    join.add_argument("--output", type=Path, default=Path("brunost-node.json"))
    join.add_argument("--hostname", default=socket.gethostname())
    join.add_argument("--capability", action="append", default=[], help="additional capability hint")
    join.add_argument("--resource-class", action="append", dest="resource_classes", default=[])
    join.add_argument("--path-map", action="append", default=[], metavar="REMOTE=LOCAL")
    join.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE")
    join.add_argument("--force", action="store_true")
    doctor = node_sub.add_parser("doctor", help="check the control plane and this node")
    doctor.add_argument("--config", type=Path, default=Path("brunost-node.json"))
    revoke = node_sub.add_parser("revoke", help="revoke a worker credential")
    revoke.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    revoke.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))
    revoke.add_argument("--worker-id", required=True)
    init = subparsers.add_parser("init", help="create a local judge project")
    init.add_argument("path", nargs="?", type=Path, default=Path("."))
    up = subparsers.add_parser("up", help="start the Docker Compose reference deployment")
    up.add_argument("--detach", action="store_true")
    up.add_argument("--file", type=Path, default=Path("docker-compose.yml"))

    canary = subparsers.add_parser("canary", help="run an end-to-end CPU judge canary")
    canary.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    canary.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))
    canary.add_argument("--task-ref", default="canary/deterministic-sum-v1")
    canary.add_argument("--task-path", type=Path, required=True)
    canary.add_argument("--submission", type=Path, required=True)
    canary.add_argument("--timeout", type=float, default=120.0)
    canary.add_argument("--poll-seconds", type=float, default=1.0)
    canary.add_argument("--queue", default="default")
    canary.add_argument("--resource-class", default="cpu")
    canary.add_argument("--callback-url", help="optional result callback URL to exercise")
    canary.add_argument("--callback-token", help="optional bearer token for the callback receiver")
    dispatcher = subparsers.add_parser("callback-dispatcher", help="deliver durable result callbacks")
    dispatcher.add_argument("--database", type=str)
    dispatcher.add_argument("--poll-seconds", default=1.0, type=float)
    dispatcher.add_argument("--worker-id")
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


def _validate_agent(path: Path, *, smoke: bool, as_json: bool) -> int:
    root = path.expanduser().resolve()
    payload: dict[str, object] = {"path": str(root), "protocol_version": protocol_spec()["protocol_version"]}
    try:
        command = resolve_agent_command(AgentSpec("validation", 0, str(root)))
        payload["command"] = list(command)
        if smoke:
            with AgentRuntime(
                (AgentSpec("validation", 0, str(root)),),
                limits=AgentLimits(startup_timeout_seconds=2, turn_timeout_seconds=1, total_timeout_seconds=5),
            ) as runtime:
                payload["runtime"] = runtime.metrics()
            payload["smoke"] = True
        else:
            payload["smoke"] = False
    except Exception as exc:  # noqa: BLE001 - CLI reports a concise validation error
        payload["valid"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
        if as_json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"invalid agent: {root}", file=sys.stderr)
            print(f"  - {payload['error']}", file=sys.stderr)
        return 2
    payload["valid"] = True
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"valid agent: {root}")
        print(f"  command: {subprocess.list2cmdline(command)}")
        if smoke:
            print("  smoke: init/ready passed")
    return 0


def _parse_agents(values: list[str]) -> tuple[AgentSeat, ...]:
    agents: list[AgentSeat] = []
    for seat, value in enumerate(values):
        if "=" not in value:
            raise ValueError(f"agent must be ID=PATH: {value}")
        agent_id, path = value.split("=", 1)
        if not agent_id or not path:
            raise ValueError(f"agent must be ID=PATH: {value}")
        agents.append(AgentSeat(agent_id, str(Path(path).expanduser().resolve()), seat))
    return tuple(agents)


def _parse_path_maps(values: list[str]) -> tuple[tuple[str, str], ...]:
    mappings: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"path map must be REMOTE=LOCAL: {value}")
        remote, local = value.split("=", 1)
        if not remote or not local:
            raise ValueError(f"path map must be REMOTE=LOCAL: {value}")
        mappings.append((remote, local))
    return tuple(mappings)


def _parse_metadata(values: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"metadata must be KEY=VALUE: {value}")
        key, item = value.split("=", 1)
        if not key:
            raise ValueError(f"metadata must be KEY=VALUE: {value}")
        metadata[key] = item
    return metadata


def _detect_capabilities() -> tuple[list[str], list[str]]:
    """Detect safe scheduling hints without requiring optional libraries."""

    capabilities = {"resource:cpu", "runtime:local", f"cpu:cores={os.cpu_count() or 1}"}
    resource_classes = {"cpu"}
    if shutil.which("docker"):
        capabilities.add("runtime:docker")
    if shutil.which("nvidia-smi") or Path("/dev/nvidia0").exists():
        capabilities.update({"gpu:true", "runtime:nvidia"})
        resource_classes.add("gpu")
    extra = os.environ.get("BRUNOST_NODE_CAPABILITIES", "")
    capabilities.update(item.strip() for item in extra.split(",") if item.strip())
    return sorted(capabilities), sorted(resource_classes)


def _write_json(path: Path, payload: dict[str, object], *, force: bool = False, private: bool = False) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise FileExistsError(f"file already exists: {path} (use --force to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if private:
        path.chmod(0o600)


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
    if args.command == "task" and args.task_command == "digest":
        validation = validate_task(args.path)
        if not validation.valid:
            return _validate(args.path)
        print(task_digest(args.path))
        return 0
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
    if args.command == "agent" and args.agent_command == "validate":
        return _validate_agent(args.path, smoke=args.smoke, as_json=args.as_json)
    if args.command == "agent" and args.agent_command == "protocol":
        if args.as_json:
            print(json.dumps(protocol_spec(), separators=(",", ":"), sort_keys=True))
        else:
            print(json.dumps(protocol_spec(), indent=2, sort_keys=True))
        return 0
    if args.command == "match" and args.match_command == "run":
        validation = validate_task(args.task)
        if not validation.valid:
            return _validate(args.task)
        if validation.kind != "game":
            print("match tasks must have kind: game", file=sys.stderr)
            return 2
        try:
            agents = _parse_agents(args.agent)
            result = run_local_match(
                args.task,
                agents,
                seed=args.seed,
                match_id=args.match_id,
                output_path=args.output,
            )
        except (OSError, ValueError) as exc:
            print(f"local match failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        encoded = json.dumps(result, sort_keys=True, indent=2)
        if args.result:
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0 if result.get("status") == "completed" else 1
    if args.command == "artifact" and args.artifact_command == "pack":
        try:
            data = pack_directory(args.path)
        except Exception as exc:  # noqa: BLE001 - CLI reports a concise packaging error
            print(f"artifact packaging failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        identifier = digest_artifact(data)
        output = args.output or args.path.with_name(f"{identifier}.tar.gz")
        output.write_bytes(data)
        print(json.dumps({"artifact_id": identifier, "path": str(output.resolve()), "size_bytes": len(data)}, sort_keys=True))
        return 0
    if args.command == "artifact" and args.artifact_command == "upload":
        try:
            result = JudgeClient(args.url, token=args.token).upload_artifact(args.path)
        except Exception as exc:  # noqa: BLE001 - CLI reports a concise upload error
            print(f"artifact upload failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "cluster" and args.cluster_command == "init":
        root = args.path.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        cluster_id = args.cluster_id or f"cluster-{uuid.uuid4().hex[:12]}"
        env_path = root / ".env"
        if env_path.exists() and not args.force:
            print(f"file already exists: {env_path} (use --force to replace it)", file=sys.stderr)
            return 2
        env_path.write_text(
            "\n".join([
                f"BRUNOST_JUDGE_CLUSTER_ID={cluster_id}",
                f"BRUNOST_JUDGE_DOMAIN={args.domain}",
                "BRUNOST_JUDGE_IMAGE=ghcr.io/mlgorithm/brunost-judge@sha256:<64-hex-digest>",
                "BRUNOST_JUDGE_SANDBOX_IMAGE=ghcr.io/brunost/judge-runtime@sha256:<64-hex-digest>",
                "BRUNOST_DOCKER_SOCKET_PROXY_IMAGE=tecnativa/docker-socket-proxy@sha256:<64-hex-digest>",
                "BRUNOST_JUDGE_CALLBACK_HOSTS=premium.example",
                "POSTGRES_DB=brunost_judge",
                "POSTGRES_USER=brunost",
                f"BRUNOST_JUDGE_API_TOKEN={new_secret()}",
                f"BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET={new_secret()}",
                "BRUNOST_JUDGE_REQUIRE_API_TOKEN=true",
                "BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true",
                "POSTGRES_PASSWORD=" + new_secret(),
                "",
            ]),
            encoding="utf-8",
        )
        env_path.chmod(0o600)
        _write_json(root / "brunost-cluster.json", {
            "version": 1,
            "name": args.name,
            "domain": args.domain,
            "cluster_id": cluster_id,
            "env_file": str(env_path),
        }, force=args.force)
        render_country_bundle(root, force=args.force)
        print(f"created cluster configuration: {root}")
        print(f"keep secrets private: {env_path}")
        return 0
    if args.command == "cluster" and args.cluster_command == "issue-node-token":
        client = JudgeClient(args.url, token=args.token)
        try:
            record = client.issue_enrollment_token(
                node_id=args.node_id,
                worker_id=args.worker_id,
                role=args.role,
                capabilities=args.capability,
                queues=args.queue or ["default"],
                resource_classes=args.resource_classes or ["cpu"],
                region=args.region,
                ttl_seconds=args.ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - operator command reports actionable failure
            print(f"unable to issue node token: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    if args.command == "node" and args.node_command == "join":
        if not args.join_token:
            print("--join-token (or BRUNOST_JUDGE_JOIN_TOKEN) is required", file=sys.stderr)
            return 2
        try:
            path_map = _parse_path_maps(args.path_map)
            metadata = _parse_metadata(args.metadata)
            detected_capabilities, detected_resources = _detect_capabilities()
            detected_capabilities = sorted(set(detected_capabilities) | set(args.capability))
            detected_resources = sorted(set(detected_resources) | set(args.resource_classes))
            response = JudgeClient(args.url).enroll_node(
                join_token=args.join_token,
                hostname=args.hostname,
                capabilities=detected_capabilities,
                resource_classes=detected_resources,
                metadata=metadata,
            )
            worker = response["worker"]
            config = {
                "version": 1,
                "api_url": args.url.rstrip("/"),
                "cluster_id": response.get("cluster_id", "local"),
                "node_id": response.get("node_id"),
                "worker_id": worker["worker_id"],
                "worker_token": response["worker_token"],
                "capabilities": worker.get("capabilities", []),
                "queues": worker.get("queues", ["default"]),
                "resource_classes": worker.get("resource_classes", ["cpu"]),
                "region": worker.get("region"),
                "path_map": [list(item) for item in path_map],
            }
            _write_json(args.output, config, force=args.force, private=True)
        except (ValueError, KeyError, FileExistsError) as exc:
            print(f"node join failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - operator command reports actionable failure
            print(f"node join failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"node joined: {worker['worker_id']}")
        print(f"worker configuration saved: {args.output.expanduser().resolve()}")
        return 0
    if args.command == "node" and args.node_command == "doctor":
        try:
            config = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
            client = JudgeClient(config["api_url"], token=config["worker_token"])
            result = {"health": client.health(), "worker": client.worker_status(config["worker_id"])}
        except Exception as exc:  # noqa: BLE001 - doctor should print one actionable failure
            print(f"node doctor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["worker"].get("status") in {"ready", "busy"} else 1
    if args.command == "node" and args.node_command == "revoke":
        try:
            result = JudgeClient(args.url, token=args.token).revoke_worker_credential(args.worker_id)
        except Exception as exc:  # noqa: BLE001 - operator command reports actionable failure
            print(f"credential revoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
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
    if args.command == "callback-dispatcher":
        from brunost_judge.store import create_store
        from brunost_judge.worker import CallbackDispatcher

        try:
            dispatcher = CallbackDispatcher(
                create_store(args.database or os.environ.get("BRUNOST_JUDGE_DATABASE_URL") or os.environ.get("BRUNOST_JUDGE_DB", "judge.db")),
                poll_seconds=args.poll_seconds,
                worker_id=args.worker_id,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed with an actionable startup error
            print(f"callback dispatcher failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        dispatcher.run_forever()
        return 0
    if args.command == "auth" and args.auth_command == "rotate-admin-token":
        destination = args.output.expanduser().resolve()
        if destination.exists() and not args.force:
            print(f"file already exists: {destination} (use --force to replace it)", file=sys.stderr)
            return 2
        try:
            write_secret_file(destination, new_secret())
        except OSError as exc:
            print(f"could not write admin token: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"token_file": str(destination), "rotated": True}, sort_keys=True))
        return 0
    if args.command == "worker":
        from brunost_judge.store import create_store
        from brunost_judge.worker import LocalWorker, RemoteWorker

        if args.config:
            try:
                config = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
                mappings = list(config.get("path_map", []))
                mappings.extend(_parse_path_maps(args.path_map))
                worker = RemoteWorker(
                    config["api_url"],
                    config["worker_token"],
                    config["worker_id"],
                    poll_seconds=args.poll_seconds,
                    path_map=tuple((str(item[0]), str(item[1])) for item in mappings),
                )
            except (OSError, KeyError, TypeError, ValueError) as exc:
                print(f"invalid node configuration: {exc}", file=sys.stderr)
                return 2
            if args.once:
                return 0 if worker.process_one() is not None else 1
            worker.run_forever()
            return 0

        worker = LocalWorker(
            create_store(args.database or os.environ.get("BRUNOST_JUDGE_DATABASE_URL") or os.environ.get("BRUNOST_JUDGE_DB", "judge.db")),
            poll_seconds=args.poll_seconds,
            worker_id=args.worker_id,
            queues=tuple(args.queues) if args.queues else None,
            resource_classes=tuple(args.resource_classes) if args.resource_classes else None,
            capabilities=tuple(args.capabilities) if args.capabilities else (),
            region=args.region,
            lease_seconds=args.lease_seconds,
        )
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
            config.write_text("version: 1\nname: my-judge\ndatabase: judge.db\nqueues:\n  - default\nresource_classes:\n  - cpu\n", encoding="utf-8")
        env = root / ".env.example"
        if not env.exists():
            env.write_text("# Required before exposing the API\nBRUNOST_JUDGE_API_TOKEN=replace-me\n# Prefer a mounted secret and set BRUNOST_JUDGE_API_TOKEN_FILE instead.\nBRUNOST_JUDGE_REQUIRE_API_TOKEN=true\n# Required for signed result callbacks\nBRUNOST_JUDGE_CALLBACK_SIGNING_SECRET=replace-with-a-long-random-secret\n# BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET_FILE=/run/secrets/brunost-callback-secret\n# BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS=true\n", encoding="utf-8")
        print(f"initialized judge project: {root}")
        return 0
    if args.command == "up":
        command = ["docker", "compose", "-f", str(args.file), "up", "--build"]
        if args.detach:
            command.append("--detach")
        return subprocess.run(command, check=False).returncode
    if args.command == "canary":
        validation = validate_task(args.task_path)
        if not validation.valid:
            return _validate(args.task_path)
        if not args.submission.is_dir():
            print(f"submission directory does not exist: {args.submission}", file=sys.stderr)
            return 2
        client = JudgeClient(args.url, token=args.token, timeout=30)
        try:
            # The canary must work when the API, worker, and operator do not
            # share a filesystem. Upload both inputs first, then use only
            # immutable content-addressed references for the execution.
            task_artifact = client.upload_artifact(args.task_path)
            task = client.register_task(
                task_ref=args.task_ref,
                artifact_id=str(task_artifact["artifact_id"]),
                kind=validation.kind,
            )
            submission_artifact = client.upload_artifact(args.submission)
            key = f"canary-{uuid.uuid4()}"
            payload = {
                "task_ref": args.task_ref,
                "submission_artifact_id": str(submission_artifact["artifact_id"]),
                "idempotency_key": key,
                "queue": args.queue,
                "resource_class": args.resource_class,
                "metadata": {"canary": True, "task_digest": task.get("manifest", {}).get("digest")},
                "callback_url": args.callback_url,
                "callback_token": args.callback_token,
            }
            first = client.submit(**payload)
            second = client.submit(**payload)
            if first.get("execution_id") != second.get("execution_id"):
                raise RuntimeError("idempotency check failed: duplicate execution IDs")
            deadline = time.monotonic() + max(1.0, args.timeout)
            current = first
            while time.monotonic() < deadline:
                current = client.get_execution(first["execution_id"])
                if current.get("status") in {"completed", "failed", "canceled"}:
                    break
                time.sleep(max(0.05, args.poll_seconds))
            checks = {
                "immutable_task_artifact": bool(task_artifact.get("artifact_id")),
                "immutable_submission_artifact": bool(submission_artifact.get("artifact_id")),
                "idempotency": first.get("execution_id") == second.get("execution_id"),
                "terminal": current.get("status") in {"completed", "failed", "canceled"},
                "completed": current.get("status") == "completed",
            }
            print(json.dumps({"checks": checks, "task": task, "execution": current}, sort_keys=True, indent=2))
            return 0 if current.get("status") == "completed" else 1
        except Exception as exc:  # noqa: BLE001 - canary should report one actionable failure
            print(f"judge canary failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 2
