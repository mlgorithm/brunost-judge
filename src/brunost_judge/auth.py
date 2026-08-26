"""Small security primitives used by the judge control plane.

The judge deliberately does not implement end-user identity.  These helpers
cover service-to-service credentials and deployment secret handling only.
"""

from __future__ import annotations

import hmac
import os
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

SERVICE_SCOPES = frozenset({"judge:read", "judge:write", "judge:admin"})
MAX_SECRET_FILE_BYTES = 4096


def configured_secret(name: str, *, required: bool = False) -> str | None:
    """Load a secret from an environment variable or its ``_FILE`` variant.

    If both forms are configured they must match.  Failing closed here avoids
    silently rotating or authenticating against a different secret than the
    operator intended.
    """

    environment_value = os.environ.get(name)
    if environment_value is not None:
        environment_value = environment_value.strip()
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    file_value: str | None = None
    if file_name:
        path = Path(file_name).expanduser()
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"could not read secret file {path}: {exc}") from exc
        if len(data) > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"secret file {path} exceeds {MAX_SECRET_FILE_BYTES} bytes")
        try:
            file_value = data.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"secret file {path} is not valid UTF-8") from exc
    if environment_value and file_value and not constant_time_equal(environment_value, file_value):
        raise RuntimeError(f"{name} and {name}_FILE contain different secrets")
    value = environment_value or file_value
    if required and not value:
        raise RuntimeError(f"{name} or {name}_FILE must be configured")
    return value or None


def constant_time_equal(left: str, right: str) -> bool:
    """Compare secret strings without an early-exit equality check."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def bearer_token(authorization: str | None) -> str | None:
    """Extract a single bearer credential from an Authorization header."""

    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


def write_secret_file(path: str | Path, value: str) -> Path:
    """Atomically write a private secret file with mode 0600."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(value.rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        os.chmod(destination, 0o600)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return destination


class RateLimiter:
    """Process-local rolling-window limiter for the API edge.

    A shared proxy or Redis-backed limiter is still required when several API
    replicas must share one limit.  This class protects a single process and
    gives a safe default for the standalone deployment.
    """

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._max_keys = 10_000

    def allow(self, key: str, bucket: str, limit: int, *, window_seconds: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        bounded_limit = max(1, int(limit))
        event_key = (bucket, key)
        with self._lock:
            events = self._events[event_key]
            cutoff = now - window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(event_key, None)
                events = deque()
                self._events[event_key] = events
            if len(events) >= bounded_limit:
                retry_after = max(1, int(events[0] + window_seconds - now + 0.999))
                return False, retry_after
            events.append(now)
            if len(self._events) > self._max_keys:
                candidates = [(key, values) for key, values in self._events.items() if key != event_key]
                if candidates:
                    oldest_key, _ = min(candidates, key=lambda item: item[1][-1] if item[1] else float("-inf"))
                    self._events.pop(oldest_key, None)
            return True, 0


def int_environment(name: str, default: int, *, minimum: int = 1, maximum: int = 100_000) -> int:
    """Read a bounded integer environment setting without crashing requests."""

    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))
