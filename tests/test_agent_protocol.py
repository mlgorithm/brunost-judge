import pytest

from brunost_judge.agent_protocol import (
    ProtocolValidationError,
    decode_message,
    encode_message,
    protocol_spec,
    validate_message,
)


def test_protocol_round_trip_and_machine_spec():
    encoded = encode_message(
        {
            "type": "turn",
            "turn": 1,
            "state": {"round": 1},
            "seed": 7,
            "agent_id": "red",
            "seat": 0,
        }
    )

    assert decode_message(b'{"type":"ready"}') == {"type": "ready"}
    assert decode_message(encoded.rstrip(b"\n"), direction="controller") == {
        "type": "turn",
        "turn": 1,
        "state": {"round": 1},
        "seed": 7,
        "agent_id": "red",
        "seat": 0,
    }
    assert protocol_spec()["protocol_version"] == 1


@pytest.mark.parametrize(
    "payload, direction, message",
    [
        ({"type": "turn", "turn": 0, "state": {}, "seed": 1, "agent_id": "a", "seat": 0}, "controller", "turn must be positive"),
        ({"type": "action"}, "agent", "action requires action"),
        ({"type": "init", "protocol_version": 2, "agent_id": "a", "seat": 0, "seed": 1}, "controller", "protocol_version must be 1"),
    ],
)
def test_protocol_rejects_invalid_messages(payload, direction, message):
    with pytest.raises(ProtocolValidationError, match=message):
        validate_message(payload, direction=direction)


def test_protocol_rejects_non_finite_json():
    with pytest.raises(ProtocolValidationError, match="non-finite"):
        decode_message(b'{"type":"action","action":NaN}')
