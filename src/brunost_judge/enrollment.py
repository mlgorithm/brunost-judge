"""Secure, one-time node enrollment helpers.

The control plane creates a short-lived join token for an operator.  The token
is only presented once by a node; the store keeps a digest and marks it used
atomically.  A successful enrollment receives a separate, revocable worker
credential for heartbeats and execution leases.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta


def new_secret() -> str:
    """Return a URL-safe secret suitable for a one-time join or worker token."""

    return secrets.token_urlsafe(32)


def digest_secret(value: str) -> str:
    """Return a stable digest; raw credentials are never persisted."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expires_at(ttl_seconds: int) -> str:
    """Return an ISO-8601 UTC expiry for a bounded enrollment token."""

    return (datetime.now(UTC) + timedelta(seconds=max(60, int(ttl_seconds)))).isoformat()


def is_expired(value: str) -> bool:
    """Check an ISO-8601 timestamp without accepting naive timestamps."""

    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= datetime.now(UTC)

