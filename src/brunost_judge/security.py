"""Small, dependency-free helpers for authenticated result callbacks."""

from __future__ import annotations

import hashlib
import hmac
import time


def callback_signature(payload: bytes, secret: str, timestamp: str | None = None) -> tuple[str, str]:
    """Return the timestamp and ``sha256=...`` signature for a callback body."""
    sent_at = timestamp or str(int(time.time()))
    digest = hmac.new(secret.encode("utf-8"), f"{sent_at}.".encode() + payload, hashlib.sha256).hexdigest()
    return sent_at, f"sha256={digest}"


def verify_callback_signature(
    payload: bytes,
    secret: str,
    signature: str,
    timestamp: str,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify a callback signature and reject stale/replayed timestamps."""
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - sent_at) > tolerance_seconds:
        return False
    _, expected = callback_signature(payload, secret, str(sent_at))
    return hmac.compare_digest(expected, signature)
