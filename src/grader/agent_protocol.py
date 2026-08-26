"""Validation and framing helpers for the versioned agent JSONL protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

PROTOCOL_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024
MESSAGE_TYPES = frozenset({"init", "ready", "turn", "action", "shutdown"})
ProtocolDirection = Literal["controller", "agent"]


class ProtocolValidationError(ValueError):
    """A JSONL message violates the public agent protocol."""


def encode_message(message: Mapping[str, Any], *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> bytes:
    """Validate and encode one controller message, including its newline frame."""

    normalized = validate_message(message, direction="controller")
    try:
        encoded = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"message is not JSON serializable: {exc}") from exc
    if len(encoded) > max_bytes:
        raise ProtocolValidationError(f"message exceeds size limit ({max_bytes} bytes)")
    return encoded


def decode_message(
    raw: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    direction: ProtocolDirection = "agent",
) -> dict[str, Any]:
    """Decode one newline-delimited UTF-8 JSON object."""

    if len(raw) > max_bytes:
        raise ProtocolValidationError(f"message exceeds size limit ({max_bytes} bytes)")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolValidationError(f"message is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolValidationError("message must be a JSON object")
    return validate_message(payload, direction=direction)


def validate_message(message: Mapping[str, Any], *, direction: ProtocolDirection) -> dict[str, Any]:
    """Validate a protocol message and retain unknown extension fields."""

    if not isinstance(message, Mapping):
        raise ProtocolValidationError("message must be a JSON object")
    payload = dict(message)
    message_type = payload.get("type")
    if message_type not in MESSAGE_TYPES:
        raise ProtocolValidationError(f"unknown message type: {message_type!r}")
    allowed = {"init", "turn", "shutdown"} if direction == "controller" else {"ready", "action"}
    if message_type not in allowed:
        raise ProtocolValidationError(f"{message_type!r} is not valid from the {direction}")

    if message_type == "init":
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolValidationError(f"protocol_version must be {PROTOCOL_VERSION}")
        _require_string(payload, "agent_id")
        _require_nonnegative_int(payload, "seat")
        _require_int(payload, "seed")
        if not isinstance(payload.get("metadata", {}), Mapping):
            raise ProtocolValidationError("metadata must be an object")
    elif message_type == "turn":
        _require_positive_int(payload, "turn")
        _require_string(payload, "agent_id")
        _require_nonnegative_int(payload, "seat")
        _require_int(payload, "seed")
        if "state" not in payload:
            raise ProtocolValidationError("turn requires state")
    elif message_type == "action" and "action" not in payload:
        raise ProtocolValidationError("action requires action")
    return payload


def protocol_spec() -> dict[str, Any]:
    """Return the machine-readable protocol summary used by the CLI and docs."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "transport": "newline-delimited UTF-8 JSON",
        "message_types": {
            "controller_to_agent": {
                "init": "start a seat and negotiate protocol version",
                "turn": "provide state for one turn",
                "shutdown": "request a clean process exit",
            },
            "agent_to_controller": {
                "ready": "acknowledge init",
                "action": "return the action for a turn",
            },
        },
        "compatibility": "unknown fields are retained and ignored by v1 consumers",
        "default_max_message_bytes": DEFAULT_MAX_MESSAGE_BYTES,
    }


def _require_string(payload: Mapping[str, Any], name: str) -> None:
    if not isinstance(payload.get(name), str) or not payload[name]:
        raise ProtocolValidationError(f"{name} must be a non-empty string")


def _reject_constant(value: str) -> None:
    raise ProtocolValidationError(f"non-finite JSON constant is not allowed: {value}")


def _require_int(payload: Mapping[str, Any], name: str) -> None:
    if isinstance(payload.get(name), bool) or not isinstance(payload.get(name), int):
        raise ProtocolValidationError(f"{name} must be an integer")


def _require_nonnegative_int(payload: Mapping[str, Any], name: str) -> None:
    _require_int(payload, name)
    if payload[name] < 0:
        raise ProtocolValidationError(f"{name} must be non-negative")


def _require_positive_int(payload: Mapping[str, Any], name: str) -> None:
    _require_int(payload, name)
    if payload[name] < 1:
        raise ProtocolValidationError(f"{name} must be positive")


__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "MESSAGE_TYPES",
    "PROTOCOL_VERSION",
    "ProtocolDirection",
    "ProtocolValidationError",
    "decode_message",
    "encode_message",
    "protocol_spec",
    "validate_message",
]
