"""Small, dependency-free helpers for authenticated result callbacks."""

from __future__ import annotations

import hashlib
import hmac
import time


def callback_signature(
    payload: bytes,
    secret: str,
    timestamp: str | None = None,
    event_id: str | None = None,
) -> tuple[str, str]:
    """Return the timestamp and signature for a callback body.

    New deliveries bind the signature to a stable event ID.  The optional
    argument keeps verification compatible with pre-0.8 workers while all
    production callers supply an event ID.
    """
    sent_at = timestamp or str(int(time.time()))
    prefix = (f"{sent_at}.{event_id}." if event_id else f"{sent_at}.").encode()
    digest = hmac.new(secret.encode("utf-8"), prefix + payload, hashlib.sha256).hexdigest()
    return sent_at, f"sha256={digest}"


def verify_callback_signature(
    payload: bytes,
    secret: str,
    signature: str,
    timestamp: str,
    *,
    event_id: str | None = None,
    tolerance_seconds: int = 300,
    require_event_id: bool = False,
) -> bool:
    """Verify a callback signature and reject stale or unidentifiable events."""
    if require_event_id and not event_id:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - sent_at) > tolerance_seconds:
        return False
    _, expected = callback_signature(payload, secret, str(sent_at), event_id)
    return hmac.compare_digest(expected, signature)
