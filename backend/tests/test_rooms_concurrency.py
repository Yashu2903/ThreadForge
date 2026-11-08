"""Races on one room, with overlap FORCED rather than hoped for.

`contended()` holds the room's row lock in a separate transaction, starts every operation (they all
queue on that lock; the helper asserts none has finished), then releases it, so the operations
really do contend. The engine in these tests has a 3 s lock_timeout, so a deadlock or a
lock-ordering bug fails the test quickly with a 500 instead of hanging.
"""

import asyncio
import logging
import random

import pytest
from sqlalchemy import text

from tests.helpers import add_member, rows, scalar
from tests.test_invitations_api import epoch, invite, new_room, redeem
from tests.test_members_api import member_path


async def contended(sessionmaker, room_id, *operations, settle=0.5):
    """Run awaitables (as coroutine objects) all queued behind a held room lock."""
    async with sessionmaker() as holder, holder.begin():
        await holder.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room_id}
        )
        tasks = [asyncio.create_task(op) for op in operations]
        await asyncio.sleep(settle)
        assert not any(t.done() for t in tasks), "operations did not queue behind the room lock"
    return await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)


def code(response):
    return response.json()["error"]["code"]


async def test_two_users_redeeming_one_invitation_produce_exactly_one_winner(
    alice, bob, carol, sessionmaker
):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    results = await contended(
        sessionmaker, room_id, redeem(bob, inv["token"]), redeem(carol, inv["token"])
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 404]
    loser = next(r for r in results if r.status_code == 404)
    assert code(loser) == "invitation_invalid"

    [used] = await rows(sessionmaker, "SELECT used_by FROM invitations")
    members = await rows(
        sessionmaker,
        "SELECT user_id FROM memberships WHERE room_id = :r AND role <> 'owner'",
        r=room_id,
    )
    assert [m["user_id"] for m in members] == [used["used_by"]]  # one membership, the winner's
    assert await epoch(sessionmaker, room_id) == 2  # exactly one consumption


async def test_the_same_user_redeeming_twice_gets_one_membership_and_two_200s(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    results = await contended(
        sessionmaker,
        room_id,
        redeem(bob, inv["token"]),
        redeem(bob, inv["token"]),  # double-click
    )
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["membership"] == results[1].json()["membership"]
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=bob.id)
        == 1
    )
    assert await epoch(sessionmaker, room_id) == 2


async def test_many_users_racing_for_one_invitation_still_one_winner(
    alice, make_actor, sessionmaker
):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    users = [await make_actor(f"Racer{i}") for i in range(6)]
    results = await contended(sessionmaker, room_id, *[redeem(u, inv["token"]) for u in users])
    assert sorted(r.status_code for r in results) == [200] + [404] * 5
    assert await epoch(sessionmaker, room_id) == 2


@pytest.mark.parametrize("round_", range(4))
async def test_redeem_versus_revoke_is_consistent_in_either_order(alice, bob, sessionmaker, round_):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    revoke_call = alice.delete(f"/rooms/{room_id}/invitations/{inv['id']}")
    redeem_response, revoke_response = await contended(
        sessionmaker, room_id, redeem(bob, inv["token"]), revoke_call
    )
    [row] = await rows(sessionmaker, "SELECT used_by, revoked_at FROM invitations")
    joined = await scalar(
        sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=bob.id
    )
    if redeem_response.status_code == 200:  # redeem won
        assert (revoke_response.status_code, code(revoke_response)) == (
            409,
            "invitation_already_redeemed",
        )
        assert row["used_by"] == bob.id and row["revoked_at"] is None and joined == 1
        assert await epoch(sessionmaker, room_id) == 2
    else:  # revoke won
        assert (revoke_response.status_code, code(redeem_response)) == (204, "invitation_invalid")
        assert row["used_by"] is None and row["revoked_at"] is not None and joined == 0
        assert await epoch(sessionmaker, room_id) == 1


@pytest.mark.parametrize("round_", range(4))
async def test_redeem_versus_remove_leaves_a_consistent_membership(
    alice, bob, sessionmaker, round_
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    inv = await invite(alice, room_id, role="viewer")
    base = await epoch(sessionmaker, room_id)
    redeem_response, remove_response = await contended(
        sessionmaker, room_id, redeem(bob, inv["token"]), alice.delete(member_path(room_id, bob))
    )
    assert (redeem_response.status_code, remove_response.status_code) == (200, 204)
    [member] = await rows(sessionmaker, "SELECT * FROM memberships WHERE user_id = :u", u=bob.id)
    [invitation] = await rows(sessionmaker, "SELECT used_by FROM invitations")
    if invitation["used_by"] is None:
        # redeem ran first: bob was still active, so nothing was consumed; then he was removed.
        assert (member["state"], await epoch(sessionmaker, room_id)) == ("removed", base + 1)
    else:
        # removal ran first: bob then legitimately rejoined with the viewer invitation.
        assert (member["state"], member["role"]) == ("active", "viewer")
        assert await epoch(sessionmaker, room_id) == base + 2
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=bob.id)
        == 1
    )


@pytest.mark.parametrize("round_", range(4))
async def test_role_change_versus_removal_is_consistent_in_either_order(
    alice, bob, sessionmaker, round_
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    base = await epoch(sessionmaker, room_id)
    patch_response, delete_response = await contended(
        sessionmaker,
        room_id,
        alice.patch(member_path(room_id, bob), json={"role": "viewer"}),
        alice.delete(member_path(room_id, bob)),
    )
    assert delete_response.status_code == 204
    [member] = await rows(
        sessionmaker, "SELECT state FROM memberships WHERE user_id = :u", u=bob.id
    )
    assert member["state"] == "removed"  # whichever order, the removal always sticks
    if patch_response.status_code == 200:  # role change first, then removal
        assert await epoch(sessionmaker, room_id) == base + 2
    else:  # removal first: the target no longer exists as an active member
        assert (patch_response.status_code, code(patch_response)) == (404, "member_not_found")
        assert await epoch(sessionmaker, room_id) == base + 1


async def test_concurrent_creates_cannot_exceed_the_invitation_cap(make_actor, sessionmaker):
    from tests.factories import make_settings

    capped = make_settings(max_active_invitations_per_room=3)
    owner = await make_actor("Owner", app_settings=capped)
    room_id = await new_room(owner)
    results = await contended(
        sessionmaker,
        room_id,
        *[owner.post(f"/rooms/{room_id}/invitations", json={}) for _ in range(8)],
    )
    assert sorted(r.status_code for r in results) == [201] * 3 + [409] * 5
    assert await scalar(sessionmaker, "SELECT count(*) FROM invitations") == 3


async def test_concurrent_redemptions_cannot_exceed_the_member_cap(make_actor, sessionmaker):
    from tests.factories import make_settings

    capped = make_settings(max_members_per_room=3)  # owner + 2
    owner = await make_actor("Owner", app_settings=capped)
    room_id = await new_room(owner)
    users = [await make_actor(f"U{i}", app_settings=capped) for i in range(5)]
    invites = [await invite(owner, room_id) for _ in users]
    results = await contended(
        sessionmaker, room_id, *[redeem(u, i["token"]) for u, i in zip(users, invites, strict=True)]
    )
    assert sorted(r.status_code for r in results) == [200, 200, 409, 409, 409]
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE state = 'active'") == 3
    )
    # Losers' invitations were not consumed.
    assert await scalar(sessionmaker, "SELECT count(*) FROM invitations WHERE used_at IS NULL") == 3


async def test_concurrent_room_creation_cannot_exceed_the_owned_rooms_cap(make_actor, sessionmaker):
    from tests.factories import make_settings

    capped = make_settings(max_owned_rooms_per_user=2)
    owner = await make_actor("Owner", app_settings=capped)
    results = await asyncio.gather(
        *[owner.post("/rooms", json={"name": f"r{i}"}) for i in range(6)]
    )
    assert sorted(r.status_code for r in results) == [201, 201, 409, 409, 409, 409]
    assert await scalar(sessionmaker, "SELECT count(*) FROM rooms") == 2


async def test_a_burst_of_mixed_random_operations_keeps_every_invariant(
    alice, make_actor, sessionmaker, caplog
):
    """80 random operations, 16 at a time, all starting queued behind the room lock. No deadlock
    (a 3 s lock_timeout would turn one into a 500), and at the end: exactly one active owner who
    is rooms.owner_id, and epoch == 1 + the number of committed access changes (one structured
    'access change' log line is emitted per bump, only after commit)."""
    caplog.set_level(logging.INFO, logger="threadforge.rooms")
    rng = random.Random(20260307)
    room_id = await new_room(alice, "Burst")
    users = [await make_actor(f"Member{i}") for i in range(6)]
    for user in users[:3]:
        await add_member(sessionmaker, room_id, user.id, rng.choice(["editor", "viewer"]))
    invites = [
        await invite(alice, room_id, role=rng.choice(["editor", "viewer"])) for _ in range(12)
    ]
    caplog.clear()

    sem = asyncio.Semaphore(16)

    async def guarded(awaitable):
        async with sem:
            return await awaitable

    operations = []
    for _ in range(80):
        kind = rng.choice(
            ["redeem", "redeem", "remove", "role", "rename", "invite", "revoke", "list"]
        )
        user = rng.choice(users)
        if kind == "redeem":
            operations.append(redeem(user, rng.choice(invites)["token"]))
        elif kind == "remove":
            operations.append(alice.delete(member_path(room_id, user)))
        elif kind == "role":
            operations.append(
                alice.patch(
                    member_path(room_id, user), json={"role": rng.choice(["editor", "viewer"])}
                )
            )
        elif kind == "rename":
            operations.append(
                alice.patch(f"/rooms/{room_id}", json={"name": f"Burst {rng.randint(0, 5)}"})
            )
        elif kind == "invite":
            operations.append(alice.post(f"/rooms/{room_id}/invitations", json={}))
        elif kind == "revoke":
            operations.append(
                alice.delete(f"/rooms/{room_id}/invitations/{rng.choice(invites)['id']}")
            )
        else:
            operations.append(user.get(f"/rooms/{room_id}/members"))

    results = await contended(
        sessionmaker, room_id, *[guarded(op) for op in operations], settle=1.0
    )

    assert not [r for r in results if r.status_code >= 500], (
        "an operation failed (deadlock/timeout?)"
    )

    owners = await rows(
        sessionmaker,
        "SELECT user_id FROM memberships WHERE room_id = :r AND role = 'owner' AND state = 'active'",
        r=room_id,
    )
    room = (await rows(sessionmaker, "SELECT owner_id, membership_epoch FROM rooms"))[0]
    assert [o["user_id"] for o in owners] == [room["owner_id"]] == [alice.id]

    access_changes = sum(
        1
        for r in caplog.records
        if any(
            f"action={a} " in r.getMessage()
            for a in ("member_joined", "member_rejoined", "member_role_changed", "member_removed")
        )
    )
    assert access_changes > 5, "the burst should have exercised real access changes"
    assert room["membership_epoch"] == 1 + access_changes

    # Every consumed invitation produced exactly one membership change for its user.
    for used in await rows(
        sessionmaker, "SELECT used_by FROM invitations WHERE used_by IS NOT NULL"
    ):
        assert (
            await scalar(
                sessionmaker,
                "SELECT count(*) FROM memberships WHERE room_id = :r AND user_id = :u",
                r=room_id,
                u=used["used_by"],
            )
            == 1
        )
    # Roles and states are always valid (the DB CHECKs would have raised), and nobody has two rows.
    assert (
        await scalar(
            sessionmaker,
            "SELECT count(*) - count(DISTINCT user_id) FROM memberships WHERE room_id = :r",
            r=room_id,
        )
        == 0
    )
