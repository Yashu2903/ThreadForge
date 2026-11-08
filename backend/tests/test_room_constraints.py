"""Part 2 database-level guarantees, tested with raw SQL (no application code).

Explains what the database enforces by itself: at most one active owner per room, an owner row is
never marked removed, removed_at matches state, used_at/used_by travel together, expires_at is
after created_at, and every foreign key is RESTRICT. "Exactly one owner matching rooms.owner_id"
is enforced by the service layer (tested in test_rooms_api.py), not here.
"""

import hashlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.helpers import execute, rows, scalar

H1 = hashlib.sha256(b"one").hexdigest()
H2 = hashlib.sha256(b"two").hexdigest()


async def user(sm, name="U"):
    await execute(sm, "INSERT INTO users (display_name) VALUES (:n)", n=name)
    return await scalar(
        sm, "SELECT id FROM users WHERE display_name = :n ORDER BY created_at DESC LIMIT 1", n=name
    )


async def room(sm, owner, name="Room"):
    await execute(sm, "INSERT INTO rooms (name, owner_id) VALUES (:n, :o)", n=name, o=owner)
    return await scalar(sm, "SELECT id FROM rooms ORDER BY created_at DESC, id DESC LIMIT 1")


async def member(sm, room_id, user_id, role="editor", state="active"):
    removed = "now()" if state == "removed" else "NULL"
    await execute(
        sm,
        f"INSERT INTO memberships (room_id, user_id, role, state, removed_at) "
        f"VALUES (:r, :u, :role, :s, {removed})",
        r=room_id,
        u=user_id,
        role=role,
        s=state,
    )


async def test_room_defaults(sessionmaker):
    o = await user(sessionmaker)
    r = await room(sessionmaker, o)
    [row] = await rows(sessionmaker, "SELECT * FROM rooms WHERE id = :r", r=r)
    assert isinstance(row["id"], uuid.UUID)
    assert row["membership_epoch"] == 1 and row["version"] == 0
    assert row["created_at"].tzinfo is not None


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "ck_rooms_name_valid"),
        (" padded", "ck_rooms_name_valid"),
        ("padded ", "ck_rooms_name_valid"),
        ("   ", "ck_rooms_name_valid"),
    ],
)
async def test_room_name_must_be_trimmed_and_nonempty(sessionmaker, name, message):
    o = await user(sessionmaker)
    with pytest.raises(IntegrityError, match=message):
        await execute(
            sessionmaker, "INSERT INTO rooms (name, owner_id) VALUES (:n, :o)", n=name, o=o
        )


async def test_room_name_length_bounds(sessionmaker):
    o = await user(sessionmaker)
    await execute(
        sessionmaker, "INSERT INTO rooms (name, owner_id) VALUES (repeat('x', 120), :o)", o=o
    )
    with pytest.raises(Exception, match="too long"):
        await execute(
            sessionmaker, "INSERT INTO rooms (name, owner_id) VALUES (repeat('x', 121), :o)", o=o
        )


async def test_epoch_must_be_positive_and_version_non_negative(sessionmaker):
    o = await user(sessionmaker)
    with pytest.raises(IntegrityError, match="ck_rooms_epoch_positive"):
        await execute(
            sessionmaker,
            "INSERT INTO rooms (name, owner_id, membership_epoch) VALUES ('a', :o, 0)",
            o=o,
        )
    with pytest.raises(IntegrityError, match="ck_rooms_version_non_negative"):
        await execute(
            sessionmaker, "INSERT INTO rooms (name, owner_id, version) VALUES ('a', :o, -1)", o=o
        )


async def test_second_active_owner_is_rejected(sessionmaker):
    a, b = await user(sessionmaker, "A"), await user(sessionmaker, "B")
    r = await room(sessionmaker, a)
    await member(sessionmaker, r, a, "owner")
    with pytest.raises(IntegrityError, match="uq_memberships_one_active_owner"):
        await member(sessionmaker, r, b, "owner")


async def test_owner_in_another_room_is_fine(sessionmaker):
    a = await user(sessionmaker, "A")
    r1, r2 = await room(sessionmaker, a, "one"), await room(sessionmaker, a, "two")
    await member(sessionmaker, r1, a, "owner")
    await member(sessionmaker, r2, a, "owner")


async def test_owner_membership_cannot_be_marked_removed(sessionmaker):
    a = await user(sessionmaker)
    r = await room(sessionmaker, a)
    await member(sessionmaker, r, a, "owner")
    with pytest.raises(IntegrityError, match="ck_memberships_owner_not_removed"):
        await execute(
            sessionmaker,
            "UPDATE memberships SET state = 'removed', removed_at = now() WHERE room_id = :r",
            r=r,
        )
    with pytest.raises(IntegrityError, match="ck_memberships_owner_not_removed"):
        await member(sessionmaker, r, await user(sessionmaker, "X"), "owner", "removed")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO memberships (room_id, user_id, role, state, removed_at) VALUES (:r, :u, 'editor', 'active', now())",
        "INSERT INTO memberships (room_id, user_id, role, state) VALUES (:r, :u, 'editor', 'removed')",
    ],
)
async def test_removed_at_must_match_state(sessionmaker, statement):
    a, b = await user(sessionmaker, "A"), await user(sessionmaker, "B")
    r = await room(sessionmaker, a)
    with pytest.raises(IntegrityError, match="ck_memberships_removed_at_matches_state"):
        await execute(sessionmaker, statement, r=r, u=b)


async def test_membership_role_and_state_allowlists(sessionmaker):
    a, b = await user(sessionmaker, "A"), await user(sessionmaker, "B")
    r = await room(sessionmaker, a)
    with pytest.raises(IntegrityError, match="ck_memberships_role_valid"):
        await member(sessionmaker, r, b, "admin")
    with pytest.raises(
        IntegrityError, match="ck_memberships_(state_valid|removed_at_matches_state)"
    ):
        await execute(
            sessionmaker,
            "INSERT INTO memberships (room_id, user_id, role, state) VALUES (:r, :u, 'editor', 'banned')",
            r=r,
            u=b,
        )


async def test_membership_is_unique_per_room_and_user(sessionmaker):
    a, b = await user(sessionmaker, "A"), await user(sessionmaker, "B")
    r = await room(sessionmaker, a)
    await member(sessionmaker, r, b)
    with pytest.raises(IntegrityError, match="pk_memberships"):
        await member(sessionmaker, r, b, "viewer")


async def invitation_insert(sm, r, creator, h=H1, extra_cols="", extra_vals="", role="editor"):
    await execute(
        sm,
        f"INSERT INTO invitations (room_id, token_hash, role, created_by, expires_at{extra_cols}) "
        f"VALUES (:r, :h, :role, :c, now() + interval '1 day'{extra_vals})",
        r=r,
        h=h,
        role=role,
        c=creator,
    )


async def test_used_at_without_used_by_is_rejected_and_vice_versa(sessionmaker):
    a, b = await user(sessionmaker, "A"), await user(sessionmaker, "B")
    r = await room(sessionmaker, a)
    with pytest.raises(IntegrityError, match="ck_invitations_used_pair"):
        await invitation_insert(sessionmaker, r, a, extra_cols=", used_at", extra_vals=", now()")
    with pytest.raises(IntegrityError, match="ck_invitations_used_pair"):
        await execute(
            sessionmaker,
            "INSERT INTO invitations (room_id, token_hash, role, created_by, expires_at, used_by) "
            "VALUES (:r, :h, 'editor', :c, now() + interval '1 day', :b)",
            r=r,
            h=H2,
            c=a,
            b=b,
        )
    await invitation_insert(
        sessionmaker, r, a, extra_cols=", used_at, used_by", extra_vals=", now(), '" + str(b) + "'"
    )


async def test_invitation_expiry_must_follow_creation(sessionmaker):
    a = await user(sessionmaker)
    r = await room(sessionmaker, a)
    with pytest.raises(IntegrityError, match="ck_invitations_expires_after_created"):
        await execute(
            sessionmaker,
            "INSERT INTO invitations (room_id, token_hash, role, created_by, created_at, expires_at) "
            "VALUES (:r, :h, 'editor', :c, now(), now() - interval '1 hour')",
            r=r,
            h=H1,
            c=a,
        )


async def test_invitation_role_cannot_be_owner_and_hash_is_unique_and_hex(sessionmaker):
    a = await user(sessionmaker)
    r = await room(sessionmaker, a)
    with pytest.raises(IntegrityError, match="ck_invitations_role_valid"):
        await invitation_insert(sessionmaker, r, a, role="owner")
    with pytest.raises(IntegrityError, match="ck_invitations_token_hash_format"):
        await invitation_insert(sessionmaker, r, a, h="not-a-sha256")
    await invitation_insert(sessionmaker, r, a)
    with pytest.raises(IntegrityError, match="uq_invitations_token_hash"):
        await invitation_insert(sessionmaker, r, a)


async def test_foreign_keys_are_restrict(sessionmaker):
    a, b = await user(sessionmaker, "A"), await user(sessionmaker, "B")
    r = await room(sessionmaker, a)
    await member(sessionmaker, r, b)
    await invitation_insert(sessionmaker, r, a)
    with pytest.raises(IntegrityError, match="fk_memberships_room_id_rooms"):
        await execute(sessionmaker, "DELETE FROM rooms WHERE id = :r", r=r)
    with pytest.raises(IntegrityError, match="fk_rooms_owner_id_users"):
        await execute(sessionmaker, "DELETE FROM users WHERE id = :a", a=a)
    with pytest.raises(IntegrityError, match="fk_memberships_user_id_users"):
        await execute(sessionmaker, "DELETE FROM users WHERE id = :b", b=b)
    await execute(sessionmaker, "DELETE FROM memberships WHERE room_id = :r", r=r)
    with pytest.raises(IntegrityError, match="fk_invitations_room_id_rooms"):
        await execute(sessionmaker, "DELETE FROM rooms WHERE id = :r", r=r)


async def test_membership_must_reference_existing_room_and_user(sessionmaker):
    a = await user(sessionmaker)
    with pytest.raises(IntegrityError, match="fk_memberships_room_id_rooms"):
        await member(sessionmaker, uuid.uuid4(), a)
