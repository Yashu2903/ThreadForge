"""Part 3 database-level guarantees, tested with raw SQL (no application code)."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.helpers import execute, rows, scalar

FINGERPRINT = "'\\x" + "ab" * 32 + "'"  # 32 bytes


async def setup(sm):
    await execute(sm, "INSERT INTO users (display_name) VALUES ('A'), ('B')")
    a = await scalar(sm, "SELECT id FROM users WHERE display_name = 'A'")
    b = await scalar(sm, "SELECT id FROM users WHERE display_name = 'B'")
    await execute(sm, "INSERT INTO rooms (name, owner_id) VALUES ('r', :a), ('r2', :a)", a=a)
    room = await scalar(sm, "SELECT id FROM rooms WHERE name = 'r'")
    other = await scalar(sm, "SELECT id FROM rooms WHERE name = 'r2'")
    return room, other, a, b


async def insert_message(
    sm,
    room,
    author,
    *,
    cmid=None,
    body="'hello'",
    fingerprint=FINGERPRINT,
    extra_cols="",
    extra_vals="",
):
    cmid = cmid or uuid.uuid4()
    await execute(
        sm,
        f"INSERT INTO messages (room_id, author_id, client_message_id, body, "
        f"initial_request_fingerprint, created_at{extra_cols}) "
        f"VALUES (:r, :a, :c, {body}, {fingerprint}, clock_timestamp(){extra_vals})",
        r=room,
        a=author,
        c=cmid,
    )
    return cmid


async def test_defaults_and_no_created_at_default(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    await insert_message(sessionmaker, room, a)
    [row] = await rows(sessionmaker, "SELECT * FROM messages")
    assert isinstance(row["id"], uuid.UUID) and row["version"] == 1
    assert (row["edited_at"], row["deleted_at"], row["deleted_by"]) == (None, None, None)
    assert row["created_at"].tzinfo is not None
    # created_at has no default, so a careless insert fails instead of using now().
    with pytest.raises(IntegrityError, match="created_at"):
        await execute(
            sessionmaker,
            "INSERT INTO messages (room_id, author_id, client_message_id, body, "
            f"initial_request_fingerprint) VALUES (:r, :a, gen_random_uuid(), 'x', {FINGERPRINT})",
            r=room,
            a=a,
        )


async def test_unique_per_room_author_and_client_message_id(sessionmaker):
    room, other_room, a, b = await setup(sessionmaker)
    cmid = await insert_message(sessionmaker, room, a)
    with pytest.raises(IntegrityError, match="uq_messages_room_author_client"):
        await insert_message(sessionmaker, room, a, cmid=cmid)
    # the same client id is fine for another author, or in another room
    await insert_message(sessionmaker, room, b, cmid=cmid)
    await insert_message(sessionmaker, other_room, a, cmid=cmid)


@pytest.mark.parametrize(
    ("body", "extra_cols", "extra_vals", "constraint"),
    [
        ("NULL", "", "", "ck_messages_body_matches_deletion"),  # null body but not deleted
        ("''", "", "", "ck_messages_body_matches_deletion"),  # empty
        ("repeat('x', 5001)", "", "", "ck_messages_body_matches_deletion"),  # too long
        (
            "'text'",
            ", deleted_at",
            ", clock_timestamp()",
            "ck_messages_(body_matches_deletion|deleted_pair)",
        ),
        (
            "NULL",
            ", deleted_at",
            ", clock_timestamp()",
            "ck_messages_deleted_pair",
        ),  # no deleted_by
    ],
)
async def test_body_and_deletion_checks(sessionmaker, body, extra_cols, extra_vals, constraint):
    room, _, a, _ = await setup(sessionmaker)
    with pytest.raises(IntegrityError, match=constraint):
        await insert_message(
            sessionmaker, room, a, body=body, extra_cols=extra_cols, extra_vals=extra_vals
        )


async def test_body_length_boundaries_in_code_points(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    await insert_message(sessionmaker, room, a, body="repeat('x', 5000)")
    await insert_message(sessionmaker, room, a, body="repeat(E'\\U0001F600', 5000)")  # 5000 emoji


async def test_a_proper_tombstone_is_allowed(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    await insert_message(
        sessionmaker,
        room,
        a,
        body="NULL",
        extra_cols=", deleted_at, deleted_by",
        extra_vals=f", clock_timestamp(), '{a}'",
    )


async def test_deleted_by_without_deleted_at_is_rejected(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    with pytest.raises(IntegrityError, match="ck_messages_deleted_pair"):
        await insert_message(
            sessionmaker, room, a, extra_cols=", deleted_by", extra_vals=f", '{a}'"
        )


async def test_version_and_fingerprint_checks(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    with pytest.raises(IntegrityError, match="ck_messages_version_positive"):
        await insert_message(sessionmaker, room, a, extra_cols=", version", extra_vals=", 0")
    for bad in ("'\\x00'", "'\\x" + "ab" * 31 + "'", "'\\x" + "ab" * 33 + "'"):
        with pytest.raises(IntegrityError, match="ck_messages_fingerprint_length"):
            await insert_message(sessionmaker, room, a, fingerprint=bad)


@pytest.mark.parametrize(
    "assignment",
    [
        "room_id = (SELECT id FROM rooms WHERE name = 'r2')",
        "author_id = (SELECT id FROM users WHERE display_name = 'B')",
        "client_message_id = gen_random_uuid()",
        "initial_request_fingerprint = '\\x" + "cd" * 32 + "'",
        "created_at = created_at + interval '1 second'",
    ],
)
async def test_immutable_columns_cannot_be_updated(sessionmaker, assignment):
    room, _, a, _ = await setup(sessionmaker)
    await insert_message(sessionmaker, room, a)
    with pytest.raises((IntegrityError, DBAPIError), match="immutable"):
        await execute(sessionmaker, f"UPDATE messages SET {assignment}")


async def test_mutable_columns_can_be_updated(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    await insert_message(sessionmaker, room, a)
    await execute(
        sessionmaker,
        "UPDATE messages SET body = 'edited', edited_at = clock_timestamp(), version = version + 1",
    )
    assert await scalar(sessionmaker, "SELECT version FROM messages") == 2


async def test_messages_survive_but_block_deletion_of_their_references(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    await insert_message(sessionmaker, room, a)
    for statement, constraint in (
        ("DELETE FROM rooms WHERE id = :r", "fk_messages_room_id_rooms|fk_memberships|violates"),
        ("DELETE FROM users WHERE id = :a", "fk_messages_author_id_users|fk_rooms_owner_id_users"),
    ):
        with pytest.raises(IntegrityError, match=constraint):
            await execute(sessionmaker, statement, r=room, a=a)
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1


async def test_removing_a_membership_never_touches_messages(sessionmaker):
    room, _, a, b = await setup(sessionmaker)
    await execute(
        sessionmaker,
        "INSERT INTO memberships (room_id, user_id, role) VALUES (:r, :b, 'editor')",
        r=room,
        b=b,
    )
    await insert_message(sessionmaker, room, b)
    await execute(
        sessionmaker,
        "UPDATE memberships SET state = 'removed', removed_at = now() WHERE user_id = :b",
        b=b,
    )
    await execute(
        sessionmaker, "DELETE FROM memberships WHERE user_id = :b", b=b
    )  # even a hard delete
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1


async def test_write_bucket_constraints(sessionmaker):
    room, _, a, _ = await setup(sessionmaker)
    insert = (
        "INSERT INTO message_write_buckets (room_id, user_id, window_start, count) "
        "VALUES (:r, :a, '2026-01-01T00:00:00Z', :c)"
    )
    with pytest.raises(IntegrityError, match="ck_message_write_buckets_count_non_negative"):
        await execute(sessionmaker, insert, r=room, a=a, c=-1)
    await execute(sessionmaker, insert, r=room, a=a, c=1)
    with pytest.raises(IntegrityError, match="pk_message_write_buckets"):
        await execute(sessionmaker, insert, r=room, a=a, c=2)
    with pytest.raises(IntegrityError, match="fk_message_write_buckets_room_id_rooms"):
        await execute(sessionmaker, insert, r=uuid.uuid4(), a=a, c=1)


async def test_check_violation_exception_text_never_contains_the_body(sessionmaker):
    """Postgres puts the failing row (the body!) in the error DETAIL; SQLAlchemy's str() with
    hide_parameters=True must not carry it, and our db_error_details() helper never reads it."""
    from app.db import db_error_details

    room, _, a, _ = await setup(sessionmaker)
    secret = "TOP-SECRET-MESSAGE-" + "x" * 5000  # violates the 5000 length CHECK
    with pytest.raises(IntegrityError) as caught:
        async with sessionmaker() as db, db.begin():
            await db.execute(
                text(
                    "INSERT INTO messages (room_id, author_id, client_message_id, body, "
                    f"initial_request_fingerprint, created_at) VALUES (:r, :a, gen_random_uuid(), :b, "
                    f"{FINGERPRINT}, clock_timestamp())"
                ),
                {"r": room, "a": a, "b": secret},
            )
    assert "TOP-SECRET-MESSAGE" not in str(caught.value)
    sqlstate, constraint = db_error_details(caught.value)
    assert (sqlstate, constraint) == ("23514", "ck_messages_body_matches_deletion")


async def test_why_clock_timestamp_and_not_now(sessionmaker):
    """The reason created_at is assigned with clock_timestamp(): now() is the time the
    transaction STARTED. A transaction that starts first but commits last stamps an OLDER time
    than a message already committed, so keyset pagination would skip it."""
    room, _, a, _ = await setup(sessionmaker)

    async def run(timestamp_expr: str):
        await execute(sessionmaker, "DELETE FROM messages")
        async with sessionmaker() as slow, slow.begin():
            await slow.execute(text("SELECT now()"))  # slow transaction starts (fixes now())
            await insert_message_in(sessionmaker, room, a, "fast", "now()")  # commits first
            await slow.execute(
                text(
                    "INSERT INTO messages (room_id, author_id, client_message_id, body, "
                    f"initial_request_fingerprint, created_at) VALUES (:r, :a, gen_random_uuid(), 'slow', "
                    f"{FINGERPRINT}, {timestamp_expr})"
                ),
                {"r": room, "a": a},
            )  # commits last
        return [
            r["body"]
            for r in await rows(sessionmaker, "SELECT body FROM messages ORDER BY created_at, id")
        ]

    # With now(): the LAST-committed message sorts FIRST (older timestamp): pagination hazard.
    assert await run("now()") == ["slow", "fast"]


async def insert_message_in(sm, room, author, body, timestamp_expr):
    await execute(
        sm,
        "INSERT INTO messages (room_id, author_id, client_message_id, body, "
        f"initial_request_fingerprint, created_at) VALUES (:r, :a, gen_random_uuid(), :b, "
        f"{FINGERPRINT}, {timestamp_expr})",
        r=room,
        a=author,
        b=body,
    )
