"""Dependency-free runtime for isolated JSONL agent processes.

The evaluator image runs the referee/plugin in a network-disabled sandbox.  This
module gives trusted referees a small, deterministic protocol for launching one
agent process per seat and exchanging bounded JSON messages over stdio.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from grader.agent_protocol import (
    DEFAULT_MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolValidationError,
    decode_message,
    encode_message,
)


class AgentRuntimeError(RuntimeError):
    """Base class for contained agent lifecycle and protocol failures."""


class AgentLaunchError(AgentRuntimeError):
    """An agent executable could not be started."""


class AgentProtocolError(AgentRuntimeError):
    """An agent emitted an invalid or oversized protocol message."""


class AgentTimeout(AgentRuntimeError):
    """An agent exceeded a startup, turn, or total runtime limit."""


class AgentCrashed(AgentRuntimeError):
    """An agent exited before completing its protocol exchange."""


@dataclass(frozen=True)
class AgentSeatMetrics:
    """Timing and request counters for one isolated seat process."""

    agent_id: str
    seat: int
    requests: int
    turns: int
    startup_seconds: float
    last_request_seconds: float
    total_seconds: float
    stderr: str
    stderr_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "seat": self.seat,
            "requests": self.requests,
            "turns": self.turns,
            "startup_seconds": round(self.startup_seconds, 6),
            "last_request_seconds": round(self.last_request_seconds, 6),
            "total_seconds": round(self.total_seconds, 6),
            "stderr": self.stderr[:2000],
            "stderr_truncated": self.stderr_truncated or len(self.stderr) > 2000,
        }


@dataclass(frozen=True)
class AgentLimits:
    """Bounds applied to each agent process and protocol exchange."""

    startup_timeout_seconds: float = 2.0
    turn_timeout_seconds: float = 1.0
    total_timeout_seconds: float = 30.0
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    max_turns: int = 10_000
    memory_bytes: int = 512 * 1024 * 1024
    file_bytes: int = 16 * 1024 * 1024
    open_files: int = 128
    stderr_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        for name in ("startup_timeout_seconds", "turn_timeout_seconds", "total_timeout_seconds"):
            if not math.isfinite(float(getattr(self, name))) or float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_message_bytes", "max_turns", "memory_bytes", "file_bytes", "open_files", "stderr_bytes"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class AgentSpec:
    """One seat's immutable artifact and optional argv override."""

    agent_id: str
    seat: int
    artifact_path: str
    command: tuple[str, ...] | None = None
    seed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("agent_id is required")
        if self.seat < 0:
            raise ValueError("seat must be non-negative")
        if not self.artifact_path:
            raise ValueError("artifact_path is required")
        if self.command is not None and (not self.command or not all(isinstance(item, str) and item for item in self.command)):
            raise ValueError("command must contain non-empty argv strings")


def _manifest_command(root: Path) -> tuple[str, ...] | None:
    manifest = root / "agent.yaml"
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("command:"):
                value = stripped.split(":", 1)[1].strip().strip("\"'")
                if value:
                    return tuple(shlex.split(value))
            if stripped.startswith("entrypoint:"):
                value = stripped.split(":", 1)[1].strip().strip("\"'")
                if value:
                    return _entrypoint_command(root, value)
    manifest_json = root / "agent.json"
    if manifest_json.is_file():
        try:
            payload = json.loads(manifest_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentLaunchError(f"invalid agent.json: {exc}") from exc
        if isinstance(payload, dict):
            command = payload.get("command")
            if isinstance(command, list) and all(isinstance(item, str) and item for item in command):
                return tuple(command)
            entrypoint = payload.get("entrypoint")
            if isinstance(entrypoint, str) and entrypoint:
                return _entrypoint_command(root, entrypoint)
    return None


def _entrypoint_command(root: Path, entrypoint: str) -> tuple[str, ...]:
    path = Path(entrypoint)
    if path.is_absolute() or ".." in path.parts:
        raise AgentLaunchError("agent entrypoint must stay inside its artifact")
    if path.suffix == ".py":
        return (sys.executable, "-u", path.as_posix())
    return (f"./{path.as_posix()}",)


def resolve_agent_command(spec: AgentSpec) -> tuple[str, ...]:
    """Resolve an explicit command, manifest command, or safe Python default."""

    root = Path(spec.artifact_path).expanduser().resolve()
    if not root.is_dir():
        raise AgentLaunchError(f"agent artifact is not a directory: {root}")
    if spec.command:
        return spec.command
    manifest_command = _manifest_command(root)
    if manifest_command:
        return manifest_command
    if (root / "agent.py").is_file():
        return (sys.executable, "-u", "agent.py")
    for candidate in ("agent", "run"):
        if (root / candidate).is_file():
            return (f"./{candidate}",)
    raise AgentLaunchError(f"agent {spec.agent_id!r} has no agent.yaml command or agent.py entrypoint")


def _agent_sandbox_enabled() -> bool:
    configured = os.environ.get("BRUNOST_JUDGE_AGENT_USE_BWRAP")
    if configured is not None:
        return configured.lower() == "true"
    return os.environ.get("BRUNOST_JUDGE_ENV", "").lower() in {"prod", "production", "staging"}


def _launch_command(
    command: tuple[str, ...],
    root: Path,
    environment: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Launch one seat in a private read-only mount namespace when enabled."""

    if not _agent_sandbox_enabled():
        return list(command), environment
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise AgentLaunchError("bubblewrap is required for production agent isolation")

    resolved_root = root.resolve()
    mapped: list[str] = []
    for argument in command:
        try:
            argument_path = Path(argument).expanduser()
            candidate = (resolved_root / argument_path).resolve() if not argument_path.is_absolute() else argument_path.resolve()
            relative = candidate.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            mapped.append(argument)
        else:
            mapped.append(f"/work/{relative.as_posix()}")

    wrapped = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--clearenv",
        "--dir",
        "/work",
        "--ro-bind",
        str(resolved_root),
        "/work",
        "--chdir",
        "/work",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--uid",
        "65533",
        "--gid",
        "65533",
    ]
    for system_root in ("/bin", "/etc", "/lib", "/lib64", "/usr", "/usr/local"):
        if Path(system_root).exists():
            wrapped.extend(("--ro-bind", system_root, system_root))
    safe_environment = {
        **{key: value for key, value in environment.items() if key != "PYTHONPATH"},
        "HOME": "/tmp",
        "LANG": environment.get("LANG", "C"),
        "LC_ALL": environment.get("LC_ALL", "C"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    for key, value in safe_environment.items():
        wrapped.extend(("--setenv", key, value))
    return [*wrapped, *mapped], {}


def _set_resource_limits(limits: AgentLimits) -> None:
    if os.name != "posix":
        return
    import resource

    cpu_seconds = max(1, math.ceil(limits.total_timeout_seconds))
    requested = (
        (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds)),
        (resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes)),
        (resource.RLIMIT_FSIZE, (limits.file_bytes, limits.file_bytes)),
        (resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files)),
    )
    for resource_kind, value in requested:
        try:
            resource.setrlimit(resource_kind, value)
        except (OSError, ValueError):
            # Some developer hosts reject RLIMIT_AS or a requested hard limit;
            # the production Docker/gVisor profile remains the enforcement layer.
            continue


class _AgentProcess:
    def __init__(self, spec: AgentSpec, limits: AgentLimits) -> None:
        self.spec = spec
        self.limits = limits
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_buffer = bytearray()
        self._deadline = 0.0
        self._started_at = 0.0
        self._startup_seconds = 0.0
        self._last_request_seconds = 0.0
        self._requests = 0
        self._turns = 0
        self._stderr_buffer = bytearray()
        self._stderr_truncated = False
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        command = resolve_agent_command(self.spec)
        allowed_environment = {
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "SYSTEMROOT",
            "WINDIR",
            "PATHEXT",
            "TMPDIR",
            "TEMP",
            "TMP",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed_environment}
        environment.update({
            "BRUNOST_AGENT_PROTOCOL": str(PROTOCOL_VERSION),
            "BRUNOST_AGENT_ID": self.spec.agent_id,
            "BRUNOST_AGENT_SEAT": str(self.spec.seat),
            "BRUNOST_AGENT_SEED": str(self.spec.seed),
        })
        launch_command, launch_environment = _launch_command(
            command,
            Path(self.spec.artifact_path).expanduser().resolve(),
            environment,
        )
        launch_options: dict[str, Any] = {}
        if os.name == "posix":
            launch_options.update(
                start_new_session=True,
                preexec_fn=lambda: _set_resource_limits(self.limits),
            )
        try:
            self.process = subprocess.Popen(
                launch_command,
                cwd=Path(self.spec.artifact_path).expanduser().resolve(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=launch_environment or environment,
                **launch_options,
            )
        except (OSError, ValueError) as exc:
            raise AgentLaunchError(f"could not launch agent {self.spec.agent_id!r}: {exc}") from exc
        if self.process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._collect_stderr,
                args=(self.process.stderr,),
                name=f"brunost-agent-stderr-{self.spec.seat}",
                daemon=True,
            )
            self._stderr_thread.start()
        self._started_at = time.monotonic()
        self._deadline = self._started_at + self.limits.total_timeout_seconds
        startup_started = time.monotonic()
        try:
            response = self.request(
                {
                    "type": "init",
                    "protocol_version": PROTOCOL_VERSION,
                    "agent_id": self.spec.agent_id,
                    "seat": self.spec.seat,
                    "seed": self.spec.seed,
                    "metadata": self.spec.metadata,
                },
                timeout=self.limits.startup_timeout_seconds,
            )
        finally:
            self._startup_seconds = time.monotonic() - startup_started
        if response.get("type") != "ready":
            raise AgentProtocolError(f"agent {self.spec.agent_id!r} did not send ready")

    def request(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise AgentRuntimeError(f"agent {self.spec.agent_id!r} is not running")
        try:
            encoded = encode_message(message, max_bytes=self.limits.max_message_bytes)
        except ProtocolValidationError as exc:
            raise AgentProtocolError(f"message to agent {self.spec.agent_id!r}: {exc}") from exc
        remaining_total = self._deadline - time.monotonic()
        if remaining_total <= 0:
            raise AgentTimeout(f"agent {self.spec.agent_id!r} exceeded total runtime")
        wait = min(timeout or self.limits.turn_timeout_seconds, remaining_total)
        request_started = time.monotonic()
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AgentCrashed(f"agent {self.spec.agent_id!r} closed stdin") from exc
        finally:
            self._requests += 1
            self._last_request_seconds = time.monotonic() - request_started
        return self._read_message(wait)

    def _read_message(self, timeout: float) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise AgentRuntimeError(f"agent {self.spec.agent_id!r} is not running")
        deadline = time.monotonic() + timeout
        fd = process.stdout.fileno()
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[: newline + 1]
                try:
                    return decode_message(raw, max_bytes=self.limits.max_message_bytes)
                except ProtocolValidationError as exc:
                    raise AgentProtocolError(f"agent {self.spec.agent_id!r}: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentTimeout(f"agent {self.spec.agent_id!r} exceeded response timeout")
            if process.poll() is not None:
                raise AgentCrashed(f"agent {self.spec.agent_id!r} exited with code {process.returncode}")
            try:
                import select

                readable, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise AgentRuntimeError(f"could not read agent {self.spec.agent_id!r}: {exc}") from exc
            if not readable:
                raise AgentTimeout(f"agent {self.spec.agent_id!r} exceeded response timeout")
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                raise AgentCrashed(f"could not read agent {self.spec.agent_id!r}: {exc}") from exc
            if not chunk:
                raise AgentCrashed(f"agent {self.spec.agent_id!r} closed stdout")
            self._stdout_buffer.extend(chunk)
            if len(self._stdout_buffer) > self.limits.max_message_bytes:
                raise AgentProtocolError(f"message from agent {self.spec.agent_id!r} exceeds size limit")

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    self.request({"type": "shutdown"}, timeout=min(0.25, self.limits.turn_timeout_seconds))
                except AgentRuntimeError:
                    pass
                if process.poll() is None:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except (PermissionError, ProcessLookupError):
                            process.terminate()
                    else:
                        process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except (PermissionError, ProcessLookupError):
                            process.kill()
                    else:
                        process.kill()
                    process.wait(timeout=0.5)
        finally:
            if self._started_at:
                self._total_seconds = time.monotonic() - self._started_at
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=0.5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (BrokenPipeError, OSError):
                        # A crashed agent may already have closed its pipe.  Cleanup
                        # must not mask the original AgentCrashed/timeout error.
                        pass

    def _collect_stderr(self, stream) -> None:  # type: ignore[no-untyped-def]
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            remaining = self.limits.stderr_bytes - len(self._stderr_buffer)
            if remaining > 0:
                self._stderr_buffer.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                self._stderr_truncated = True

    def record_turn(self) -> None:
        self._turns += 1

    def metrics(self) -> AgentSeatMetrics:
        total_seconds = getattr(self, "_total_seconds", 0.0)
        if self._started_at and not total_seconds:
            total_seconds = time.monotonic() - self._started_at
        return AgentSeatMetrics(
            agent_id=self.spec.agent_id,
            seat=self.spec.seat,
            requests=self._requests,
            turns=self._turns,
            startup_seconds=self._startup_seconds,
            last_request_seconds=self._last_request_seconds,
            total_seconds=total_seconds,
            stderr=bytes(self._stderr_buffer).decode("utf-8", errors="replace"),
            stderr_truncated=self._stderr_truncated,
        )


class AgentRuntime:
    """Run one bounded agent process per deterministic match seat."""

    def __init__(self, specs: tuple[AgentSpec, ...], *, limits: AgentLimits | None = None, seed: int = 0) -> None:
        if not specs:
            raise ValueError("at least one agent seat is required")
        seats = [spec.seat for spec in specs]
        if len(set(seats)) != len(seats):
            raise ValueError("agent seats must be unique")
        self.specs = tuple(sorted(specs, key=lambda item: item.seat))
        self.limits = limits or AgentLimits()
        self.seed = seed
        self._processes: dict[int, _AgentProcess] = {}
        self._closed_metrics: dict[int, AgentSeatMetrics] = {}
        self._turn = 0
        self._started = False
        self._started_at = 0.0
        self._total_seconds = 0.0

    @classmethod
    def from_context(cls, context: dict[str, Any], *, limits: AgentLimits | None = None) -> AgentRuntime:
        participants = context.get("participants")
        seats = context.get("seats")
        if not isinstance(participants, dict) or not isinstance(seats, list):
            raise TypeError("agent runtime context needs participants and seats")
        seed = context.get("seed") if isinstance(context.get("seed"), int) else 0
        specs: list[AgentSpec] = []
        for item in seats:
            if not isinstance(item, dict):
                raise TypeError("agent seat must be an object")
            agent_id = item.get("agent_id")
            seat = item.get("seat")
            path = participants.get(agent_id)
            if not isinstance(agent_id, str) or not isinstance(seat, int) or not isinstance(path, str):
                raise TypeError("agent seat requires agent_id, seat, and participant path")
            seat_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            command = item.get("command", seat_metadata.get("command"))
            if isinstance(command, str):
                command = tuple(shlex.split(command))
            elif isinstance(command, list):
                command = tuple(command)
            else:
                command = None
            specs.append(AgentSpec(agent_id, seat, path, command=command, seed=seed, metadata=seat_metadata))
        return cls(tuple(specs), limits=limits, seed=seed)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def start(self) -> None:
        if self._started:
            return
        self._started_at = time.monotonic()
        try:
            for spec in self.specs:
                process = _AgentProcess(spec, self.limits)
                self._processes[spec.seat] = process
                process.start()
        except Exception:
            self.close()
            raise
        self._started = True

    def request(self, seat: int, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if seat not in self._processes:
            raise AgentRuntimeError(f"unknown or inactive agent seat: {seat}")
        return self._processes[seat].request(message, timeout=timeout)

    def step(self, state: Any, *, turn: int | None = None, simultaneous: bool = False) -> dict[int, Any]:
        """Send one turn to every seat and return actions in seat order.

        Sequential mode preserves referee seat order. Simultaneous mode sends
        the same turn to all seats concurrently so one agent cannot observe
        another agent's response latency.
        """

        if not self._started:
            raise AgentRuntimeError("agent runtime is not started")
        next_turn = self._turn + 1 if turn is None else int(turn)
        if next_turn < 1 or next_turn > self.limits.max_turns:
            raise AgentTimeout("match exceeded maximum turns")
        self._turn = next_turn
        messages = {
            spec.seat: {
                "type": "turn",
                "turn": next_turn,
                "state": state,
                "seed": self.seed,
                "agent_id": spec.agent_id,
                "seat": spec.seat,
            }
            for spec in self.specs
        }
        if simultaneous:
            with ThreadPoolExecutor(max_workers=len(self.specs), thread_name_prefix="brunost-agent") as pool:
                futures = {seat: pool.submit(self.request, seat, message) for seat, message in messages.items()}
                responses = {seat: futures[seat].result() for seat in sorted(futures)}
        else:
            responses = {seat: self.request(seat, messages[seat]) for seat in sorted(messages)}
        actions: dict[int, Any] = {}
        for spec in self.specs:
            response = responses[spec.seat]
            if response.get("type") != "action" or "action" not in response:
                raise AgentProtocolError(f"agent {spec.agent_id!r} seat {spec.seat} did not return an action")
            actions[spec.seat] = response["action"]
            self._processes[spec.seat].record_turn()
        return actions

    def close(self) -> None:
        for process in reversed(tuple(self._processes.values())):
            process.close()
            self._closed_metrics[process.spec.seat] = process.metrics()
        self._processes.clear()
        self._started = False
        if self._started_at:
            self._total_seconds = time.monotonic() - self._started_at

    def metrics(self) -> dict[str, Any]:
        """Return bounded runtime telemetry suitable for match metrics."""

        seats = dict(self._closed_metrics)
        seats.update({seat: process.metrics() for seat, process in self._processes.items()})
        elapsed = self._total_seconds
        if self._started and self._started_at:
            elapsed = time.monotonic() - self._started_at
        return {
            "protocol_version": PROTOCOL_VERSION,
            "seed": self.seed,
            "turns": self._turn,
            "elapsed_seconds": round(elapsed, 6),
            "seats": {str(seat): metrics.as_dict() for seat, metrics in sorted(seats.items())},
        }


__all__ = [
    "PROTOCOL_VERSION",
    "AgentCrashed",
    "AgentLaunchError",
    "AgentLimits",
    "AgentProtocolError",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentSeatMetrics",
    "AgentSpec",
    "AgentTimeout",
    "resolve_agent_command",
]
