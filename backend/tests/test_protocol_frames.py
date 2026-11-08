"""Client frame validation: strict, bounded, and never echoing the content."""

import json

import pytest

from app.realtime import protocol
from app.realtime.protocol import FrameError, parse_client_text


def test_valid_frames_parse():
    assert parse_client_text('{"type":"resume","schema_version":1,"after_seq":0}').after_seq == 0
    assert parse_client_text('{"type":"ack","through_seq":41}').through_seq == 41
    assert parse_client_text('{"type":"heartbeat"}').type == "heartbeat"
    assert parse_client_text('{"type":"typing","state":"stop"}').state == "stop"
    assert parse_client_text('{"type":"set_availability","value":"dnd"}').value == "dnd"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"resume"',
        "null",
        "{}",
        '{"type":42}',
        '{"type":"nope"}',
        '{"type":"resume","schema_version":1}',  # missing after_seq
        '{"type":"resume","schema_version":2,"after_seq":0}',  # unsupported version
        '{"type":"resume","schema_version":1,"after_seq":-1}',
        '{"type":"resume","schema_version":1,"after_seq":1.0}',
        '{"type":"resume","schema_version":1,"after_seq":"3"}',
        '{"type":"resume","schema_version":1,"after_seq":true}',
        '{"type":"resume","schema_version":1,"after_seq":9223372036854775808}',
        '{"type":"resume","schema_version":1,"after_seq":0,"room_id":"x"}',  # identity smuggling
        '{"type":"ack","through_seq":null}',
        '{"type":"ack","through_seq":-5}',
        '{"type":"ack","through_seq":1,"user_id":"x"}',
        '{"type":"heartbeat","extra":1}',
        '{"type":"typing","state":"START"}',
        '{"type":"typing","state":"start","user_id":"x"}',
        '{"type":"set_availability","value":"away"}',
        '{"type":"set_availability"}',
    ],
)
def test_invalid_frames_are_protocol_violations(raw):
    with pytest.raises(FrameError) as caught:
        parse_client_text(raw)
    assert caught.value.code == protocol.CLOSE_PROTOCOL_VIOLATION


def test_oversize_is_measured_in_bytes_and_closes_1009():
    just_fits = json.dumps({"type": "heartbeat", "pad": "x" * 10})
    assert len(just_fits) < 1024
    big = '{"type":"heartbeat","pad":"' + "é" * 600 + '"}'  # < 1024 characters, > 1024 bytes
    assert len(big) < 1024 < len(big.encode())
    with pytest.raises(FrameError) as caught:
        parse_client_text(big)
    assert caught.value.code == protocol.CLOSE_MESSAGE_TOO_BIG


def test_errors_never_echo_the_frame():
    with pytest.raises(FrameError) as caught:
        parse_client_text('{"type":"ack","through_seq":"SECRET-VALUE"}')
    assert "SECRET-VALUE" not in str(caught.value) and "SECRET-VALUE" not in caught.value.reason


def test_close_code_table_is_complete_and_1008_is_not_part_of_the_part5_table():
    assert set(protocol.CLOSE_CODE_NAMES) == {
        1001, 1003, 1009, 1013, 4400, 4401, 4403, 4408, 4409, 4410, 4429,
    }  # fmt: skip


def test_server_frames_have_the_documented_shape():
    import uuid

    room = uuid.uuid4()
    user = uuid.uuid4()
    assert json.loads(protocol.sync_started(room, 3, 9)) == {
        "type": "sync_started",
        "schema_version": 1,
        "room_id": str(room),
        "after_seq": 3,
        "through_seq": 9,
    }
    assert json.loads(protocol.sync_complete(9)) == {"type": "sync_complete", "through_seq": 9}
    ack = json.loads(protocol.heartbeat_ack())
    assert set(ack) == {"type", "server_time"} and ack["type"] == "heartbeat_ack"
    assert json.loads(protocol.presence_snapshot(room, [(user, "busy")], False)) == {
        "type": "presence_snapshot",
        "room_id": str(room),
        "online": [{"user_id": str(user), "availability": "busy"}],
        "degraded": False,
    }
    assert json.loads(protocol.typing_state(room, [user], True)) == {
        "type": "typing_state",
        "room_id": str(room),
        "typing": [str(user)],
        "degraded": True,
    }
    assert json.loads(protocol.resync_required("event_gap")) == {
        "type": "resync_required",
        "reason": "event_gap",
    }
