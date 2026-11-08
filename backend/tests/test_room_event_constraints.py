"""Database-level guarantees of room_events and outbox, tested with raw SQL."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.helpers import execute, rows, scalar

FOUR_STATES_OK = {
    "pending": "state, claim_token, claimed_until, processed_at",
    "processing": "state, claim_token, claimed_until, processed_at",
}


async def setup(sm):
    await execute(sm, "INSERT INTO users (display_name) VALUES ('A')")
    user = await scalar(sm, "SELECT id FROM users")
    await execute(sm, "INSERT INTO rooms (name, owner_id) VALUES ('r', :u), ('r2', :u)", u=user)
    room = await scalar(sm, "SELECT id FROM rooms WHERE name = 'r'")
    other = await scalar(sm, "SELECT id FROM rooms WHERE name = 'r2'")
    return room, other, user


async def add_event(
    sm, room, user, seq, *, etype="room_created", payload="'{}'", extra_cols="", extra_vals=""
):
    await execute(
        sm,
        f"INSERT INTO room_events (room_id, seq, type, resource_type, resource_id, actor_id, payload, "
        f"created_at{extra_cols}) VALUES (:r, :s, '{etype}', 'room', :r, :u, CAST({payload} AS jsonb), "
        f"clock_timestamp(){extra_vals})",
        r=room,
        s=seq,
        u=user,
    )


async def add_outbox(sm, room, seq, event_id, *, cols="", vals=""):
    await execute(
        sm,
        f"INSERT INTO outbox (event_id, room_id, room_seq, payload{cols}) "
        f"VALUES (:e, :r, :s, CAST('{{}}' AS jsonb){vals})",
        e=event_id,
        r=room,
        s=seq,
    )


async def event_id_of(sm, room, seq):
    return await scalar(
        sm, "SELECT event_id FROM room_events WHERE room_id = :r AND seq = :s", r=room, s=seq
    )


async def test_defaults_and_existing_rooms_start_at_zero_with_no_events(sessionmaker):
    room, _, user = await setup(sessionmaker)
    assert await scalar(sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room) == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM room_events") == 0  # nothing fabricated
    await add_event(sessionmaker, room, user, 1)
    [event] = await rows(sessionmaker, "SELECT * FROM room_events")
    assert isinstance(event["event_id"], uuid.UUID) and event["schema_version"] == 1
    assert event["created_at"].tzinfo is not None


async def test_event_seq_cannot_be_negative(sessionmaker):
    room, _, _ = await setup(sessionmaker)
    with pytest.raises(IntegrityError, match="ck_rooms_event_seq_non_negative"):
        await execute(sessionmaker, "UPDATE rooms SET event_seq = -1 WHERE id = :r", r=room)


async def test_primary_key_and_uniqueness(sessionmaker):
    room, other, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    with pytest.raises(IntegrityError, match="pk_room_events"):
        await add_event(sessionmaker, room, user, 1)  # same (room, seq)
    await add_event(sessionmaker, other, user, 1)  # same seq in ANOTHER room is fine
    eid = await event_id_of(sessionmaker, room, 1)
    with pytest.raises(IntegrityError, match="uq_room_events_event_id"):
        await add_event(
            sessionmaker, room, user, 2, extra_cols=", event_id", extra_vals=f", '{eid}'"
        )


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        ({"seq": 0}, "ck_room_events_seq_positive"),
        ({"seq": -5}, "ck_room_events_seq_positive"),
        ({"seq": 1, "etype": "invitation_created"}, "ck_room_events_type_valid"),
        ({"seq": 1, "payload": "'[]'"}, "ck_room_events_payload_is_object"),
        ({"seq": 1, "payload": "'\"text\"'"}, "ck_room_events_payload_is_object"),
        ({"seq": 1, "payload": "'null'"}, "ck_room_events_payload_is_object"),
        (
            {"seq": 1, "payload": "jsonb_build_object('k', repeat('x', 2100))"},
            "ck_room_events_payload_size",
        ),
    ],
)
async def test_room_event_checks(sessionmaker, kwargs, constraint):
    room, _, user = await setup(sessionmaker)
    with pytest.raises(IntegrityError, match=constraint):
        await add_event(sessionmaker, room, user, **kwargs)


async def test_payload_size_boundary(sessionmaker):
    room, _, user = await setup(sessionmaker)
    # jsonb prints {"k": "xxxx"}: 9 bytes of structure; pad the value to land exactly on 2048
    await add_event(
        sessionmaker, room, user, 1, payload="jsonb_build_object('k', repeat('x', 2048 - 9))"
    )
    assert await scalar(sessionmaker, "SELECT octet_length(payload::text) FROM room_events") == 2048


async def test_actor_is_required_and_foreign_keys_are_restrict(sessionmaker):
    room, _, user = await setup(sessionmaker)
    with pytest.raises(IntegrityError, match="actor_id"):
        await execute(
            sessionmaker,
            "INSERT INTO room_events (room_id, seq, type, resource_type, actor_id, payload, created_at) "
            "VALUES (:r, 1, 'room_created', 'room', NULL, '{}', clock_timestamp())",
            r=room,
        )
    await add_event(sessionmaker, room, user, 1)
    with pytest.raises(IntegrityError, match="fk_room_events_room_id_rooms|fk_room_events"):
        await execute(sessionmaker, "DELETE FROM rooms WHERE id = :r", r=room)
    with pytest.raises(IntegrityError, match="fk_room_events_actor_id_users|fk_rooms_owner"):
        await execute(sessionmaker, "DELETE FROM users WHERE id = :u", u=user)


async def test_composite_foreign_key_rejects_a_mismatched_event_id_and_seq(sessionmaker):
    """Two independent foreign keys would each be satisfied by a DIFFERENT event row. The single
    composite key (room_id, room_seq, event_id) forces all three to come from the same row."""
    room, _, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    await add_event(sessionmaker, room, user, 2)
    event_1 = await event_id_of(sessionmaker, room, 1)
    event_2 = await event_id_of(sessionmaker, room, 2)
    # event_1's id with event_2's sequence: each value exists, but never together
    with pytest.raises(IntegrityError, match="fk_outbox_room_event"):
        await add_outbox(sessionmaker, room, 2, event_1)
    await add_outbox(sessionmaker, room, 1, event_1)  # the matching pair is accepted
    await add_outbox(sessionmaker, room, 2, event_2)
    with pytest.raises(IntegrityError, match="fk_outbox_room_event"):  # an unknown event id
        await add_outbox(sessionmaker, room, 1, uuid.uuid4())


async def test_outbox_event_must_belong_to_the_named_room(sessionmaker):
    room, other, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    eid = await event_id_of(sessionmaker, room, 1)
    with pytest.raises(IntegrityError, match="fk_outbox_room_event"):
        await add_outbox(sessionmaker, other, 1, eid)


@pytest.mark.parametrize(
    ("state", "extra_cols", "extra_vals", "ok"),
    [
        ("pending", "", "", True),
        ("pending", ", claim_token", ", gen_random_uuid()", False),
        ("pending", ", claimed_until", ", now()", False),
        ("pending", ", processed_at", ", now()", False),
        ("processing", "", "", False),
        ("processing", ", claim_token", ", gen_random_uuid()", False),
        ("processing", ", claim_token, claimed_until", ", gen_random_uuid(), now()", True),
        (
            "processing",
            ", claim_token, claimed_until, processed_at",
            ", gen_random_uuid(), now(), now()",
            False,
        ),
        ("processed", "", "", False),
        ("processed", ", processed_at", ", now()", True),
        ("processed", ", processed_at, claim_token", ", now(), gen_random_uuid()", False),
        ("failed", "", "", False),
        ("failed", ", last_error_code", ", 'envelope_invalid'", True),
        ("failed", ", last_error_code, claim_token", ", 'x', gen_random_uuid()", False),
        ("bogus", "", "", False),
    ],
)
async def test_outbox_state_fields_must_be_consistent(
    sessionmaker, state, extra_cols, extra_vals, ok
):
    room, _, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    eid = await event_id_of(sessionmaker, room, 1)
    insert = lambda: add_outbox(  # noqa: E731
        sessionmaker, room, 1, eid, cols=", state" + extra_cols, vals=f", '{state}'" + extra_vals
    )
    if ok:
        await insert()
        assert await scalar(sessionmaker, "SELECT state FROM outbox") == state
    else:
        with pytest.raises(IntegrityError, match="ck_outbox_(state_fields_consistent|state_valid)"):
            await insert()


async def test_pending_rows_may_carry_a_last_error_code_for_retries(sessionmaker):
    room, _, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    eid = await event_id_of(sessionmaker, room, 1)
    await add_outbox(sessionmaker, room, 1, eid, cols=", last_error_code", vals=", 'redis_timeout'")


async def test_outbox_defaults_and_attempts_check(sessionmaker):
    room, _, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    eid = await event_id_of(sessionmaker, room, 1)
    await add_outbox(sessionmaker, room, 1, eid)
    [row] = await rows(sessionmaker, "SELECT * FROM outbox")
    assert (row["state"], row["attempts"], row["kind"]) == ("pending", 0, "room_event")
    assert row["next_attempt_at"] is not None and row["created_at"] is not None
    with pytest.raises(IntegrityError, match="ck_outbox_attempts_non_negative"):
        await execute(sessionmaker, "UPDATE outbox SET attempts = -1")


async def test_outbox_payload_must_be_an_object_and_kind_is_restricted(sessionmaker):
    room, _, user = await setup(sessionmaker)
    await add_event(sessionmaker, room, user, 1)
    eid = await event_id_of(sessionmaker, room, 1)
    with pytest.raises(IntegrityError, match="ck_outbox_payload_is_object"):
        await execute(
            sessionmaker,
            "INSERT INTO outbox (event_id, room_id, room_seq, payload) VALUES (:e, :r, 1, '[]')",
            e=eid,
            r=room,
        )
    with pytest.raises(IntegrityError, match="ck_outbox_kind_valid"):
        await add_outbox(sessionmaker, room, 1, eid, cols=", kind", vals=", 'something_else'")


async def test_partial_indexes_exist_for_each_state(sessionmaker):
    indexes = {
        r["indexname"]: r["indexdef"]
        for r in await rows(
            sessionmaker, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'outbox'"
        )
    }
    expected = {
        "ix_outbox_pending_next_attempt_at": ("next_attempt_at", "'pending'"),
        "ix_outbox_processing_claimed_until": ("claimed_until", "'processing'"),
        "ix_outbox_processed_processed_at": ("processed_at", "'processed'"),
        "ix_outbox_failed_created_at": ("created_at", "'failed'"),
    }
    for name, (column, state) in expected.items():
        definition = indexes[name]
        assert f"({column})" in definition and "WHERE" in definition and state in definition, (
            definition
        )
