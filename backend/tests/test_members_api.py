"""Role changes and removal: permissions, protection of the owner, epoch accounting, rollback."""

import pytest

from app.services import invitations as invitations_service
from app.services import memberships as memberships_service
from tests.helpers import add_member, rows, scalar
from tests.test_invitations_api import epoch, invite, new_room, redeem


async def membership(sm, room_id, user_id):
    [row] = await rows(
        sm, "SELECT * FROM memberships WHERE room_id = :r AND user_id = :u", r=room_id, u=user_id
    )
    return row


async def setup_room(alice, bob, sessionmaker, role="editor"):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, role)
    return room_id


def member_path(room_id, user):
    return f"/rooms/{room_id}/members/{user.id if hasattr(user, 'id') else user}"


# ---- role changes -------------------------------------------------------------------------------


async def test_owner_changes_a_role_and_the_change_is_visible_everywhere(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker, "editor")
    before = await membership(sessionmaker, room_id, bob.id)

    response = await alice.patch(member_path(room_id, bob), json={"role": "viewer"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"user_id", "display_name", "role", "joined_at"}
    assert (body["user_id"], body["role"], body["display_name"]) == (str(bob.id), "viewer", "Bob")

    after = await membership(sessionmaker, room_id, bob.id)
    assert after["role"] == "viewer" and after["state"] == "active"
    assert after["joined_at"] == before["joined_at"]  # a role change is not a rejoin
    assert await epoch(sessionmaker, room_id) == 2
    assert (await bob.get(f"/rooms/{room_id}")).json()["role"] == "viewer"
    roles = {
        m["display_name"]: m["role"]
        for m in (await alice.get(f"/rooms/{room_id}/members")).json()["items"]
    }
    assert roles == {"Alice": "owner", "Bob": "viewer"}

    assert (
        await alice.patch(member_path(room_id, bob), json={"role": "editor"})
    ).status_code == 200
    assert (await membership(sessionmaker, room_id, bob.id))["role"] == "editor"
    assert await epoch(sessionmaker, room_id) == 3


async def test_demoted_member_immediately_loses_owner_only_abilities(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker, "editor")
    await alice.patch(member_path(room_id, bob), json={"role": "viewer"})
    # The role is read from the database on every request: no stale session-held role.
    assert (await bob.get(f"/rooms/{room_id}")).json()["role"] == "viewer"
    assert (await bob.patch(f"/rooms/{room_id}", json={"name": "x"})).status_code == 403


async def test_setting_the_current_role_is_a_no_op(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker, "editor")
    room_before = await rows(
        sessionmaker, "SELECT membership_epoch, version, updated_at FROM rooms"
    )
    member_before = await membership(sessionmaker, room_id, bob.id)
    response = await alice.patch(member_path(room_id, bob), json={"role": "editor"})
    assert response.status_code == 200 and response.json()["role"] == "editor"
    assert (
        await rows(sessionmaker, "SELECT membership_epoch, version, updated_at FROM rooms")
        == room_before
    )
    assert await membership(sessionmaker, room_id, bob.id) == member_before  # row untouched


async def test_owner_cannot_be_demoted_even_by_self_targeting(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker)
    for role in ("viewer", "editor"):
        response = await alice.patch(member_path(room_id, alice), json={"role": role})
        assert (response.status_code, response.json()["error"]["code"]) == (409, "owner_protected")
    owner = await membership(sessionmaker, room_id, alice.id)
    assert (owner["role"], owner["state"]) == ("owner", "active")
    assert await epoch(sessionmaker, room_id) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "owner"},
        {"role": "admin"},
        {"role": ""},
        {"role": None},
        {"role": 1},
        {},
        {"role": "viewer", "user_id": "00000000-0000-0000-0000-000000000001"},
        {"role": "viewer", "owner_id": "00000000-0000-0000-0000-000000000001"},
        {"role": "viewer", "state": "removed"},
    ],
)
async def test_role_update_cannot_create_an_owner_or_smuggle_fields(
    alice, bob, sessionmaker, payload
):
    room_id = await setup_room(alice, bob, sessionmaker, "editor")
    response = await alice.patch(member_path(room_id, bob), json=payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    assert (await membership(sessionmaker, room_id, bob.id))["role"] == "editor"
    assert await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE role = 'owner'") == 1


async def test_role_change_target_must_be_an_active_member_of_this_room(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, sessionmaker)
    other_room = await new_room(carol, "carol's")
    await add_member(sessionmaker, other_room, bob.id, "editor")
    await add_member(sessionmaker, room_id, carol.id, "viewer", "removed")  # carol: removed here
    targets = {
        "removed": carol.id,
        "unknown": "00000000-0000-0000-0000-000000000009",
        "malformed": "not-a-uuid",
    }
    for label, target in targets.items():
        response = await alice.patch(member_path(room_id, target), json={"role": "viewer"})
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "member_not_found",
        ), label
    # Cross-room: carol owns `other_room`; naming HER as a target in alice's room cannot reach it.
    assert (await membership(sessionmaker, other_room, carol.id))["role"] == "owner"
    assert (await membership(sessionmaker, other_room, bob.id))["role"] == "editor"


async def test_cannot_touch_another_rooms_member_through_my_room(alice, bob, carol, sessionmaker):
    mine = await new_room(alice, "mine")
    theirs = await new_room(carol, "theirs")
    await add_member(sessionmaker, theirs, bob.id, "editor")  # bob belongs ONLY to carol's room
    response = await alice.patch(member_path(mine, bob), json={"role": "viewer"})
    assert (response.status_code, response.json()["error"]["code"]) == (404, "member_not_found")
    assert (await alice.delete(member_path(mine, bob))).status_code == 404
    assert (await membership(sessionmaker, theirs, bob.id))["role"] == "editor"
    assert (await membership(sessionmaker, theirs, bob.id))["state"] == "active"
    # and alice (not a member of `theirs`) cannot use that room's path at all
    assert (await alice.patch(member_path(theirs, bob), json={"role": "viewer"})).status_code == 404


async def test_member_management_permission_matrix(alice, bob, carol, make_actor, sessionmaker):
    room_id = await new_room(alice)
    victim = bob
    await add_member(sessionmaker, room_id, victim.id, "viewer")
    editor = await make_actor("Editor")  # an editor who tries to manage
    await add_member(sessionmaker, room_id, editor.id, "editor")
    path = member_path(room_id, victim)

    for actor in (editor, victim):  # editor and viewer
        for call in (
            actor.patch(path, json={"role": "editor"}),
            actor.delete(path),
        ):
            response = await call
            assert (response.status_code, response.json()["error"]["code"]) == (
                403,
                "insufficient_role",
            )
        # a non-owner MEMBER with an invalid body is 403, not 422
        assert (await actor.patch(path, json={"role": "owner"})).status_code == 403
    for call in (carol.patch(path, json={"role": "editor"}), carol.delete(path)):
        response = await call
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert (
        await carol.patch(path, json={"role": "owner"})
    ).status_code == 404  # nonmember + bad body
    assert (await membership(sessionmaker, room_id, victim.id))["role"] == "viewer"
    assert (await membership(sessionmaker, room_id, victim.id))["state"] == "active"
    assert await epoch(sessionmaker, room_id) == 1


async def test_member_management_requires_csrf_and_authentication(alice, bob, client, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker)
    path = member_path(room_id, bob)
    for call in (
        alice.patch(path, json={"role": "viewer"}, csrf=False),
        alice.delete(path, csrf=False),
    ):
        assert (await call).json()["error"]["code"] == "csrf_failed"
    assert (await client.patch(path, json={"role": "viewer"})).status_code == 401
    assert (await client.delete(path)).status_code == 401
    assert (await membership(sessionmaker, room_id, bob.id))["role"] == "editor"


# ---- removal ------------------------------------------------------------------------------------


async def test_removal_blocks_every_later_http_access(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker, "editor")
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 200

    response = await alice.delete(member_path(room_id, bob))
    assert response.status_code == 204 and response.content == b""
    row = await membership(sessionmaker, room_id, bob.id)
    assert row["state"] == "removed" and row["removed_at"] is not None  # row retained
    assert await epoch(sessionmaker, room_id) == 2

    attempts = [
        bob.get(f"/rooms/{room_id}"),
        bob.get(f"/rooms/{room_id}/members"),
        bob.get(f"/rooms/{room_id}/invitations"),
        bob.patch(f"/rooms/{room_id}", json={"name": "still here?"}),
        bob.post(f"/rooms/{room_id}/invitations", json={}),
        bob.patch(member_path(room_id, alice), json={"role": "viewer"}),
        bob.delete(member_path(room_id, alice)),
    ]
    for call in attempts:
        response = await call
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert [r["id"] for r in (await bob.get("/rooms")).json()["items"]] == []
    assert (await membership(sessionmaker, room_id, alice.id))["role"] == "owner"  # untouched


async def test_removal_is_idempotent_and_bumps_the_epoch_only_once(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker)
    path = member_path(room_id, bob)
    assert (await alice.delete(path)).status_code == 204
    first = await membership(sessionmaker, room_id, bob.id)
    for _ in range(2):
        assert (await alice.delete(path)).status_code == 204
    assert await membership(sessionmaker, room_id, bob.id) == first  # unchanged, incl. removed_at
    assert await epoch(sessionmaker, room_id) == 2


async def test_removal_target_errors(alice, bob, sessionmaker):
    room_id = await setup_room(alice, bob, sessionmaker)
    for target in ("00000000-0000-0000-0000-000000000009", "garbage"):
        response = await alice.delete(member_path(room_id, target))
        assert (response.status_code, response.json()["error"]["code"]) == (404, "member_not_found")
    response = await alice.delete(member_path(room_id, alice))  # the owner, by self-targeting
    assert (response.status_code, response.json()["error"]["code"]) == (409, "owner_protected")
    owner = await membership(sessionmaker, room_id, alice.id)
    assert (owner["state"], owner["removed_at"]) == ("active", None)
    assert await epoch(sessionmaker, room_id) == 1


async def test_a_used_invitation_cannot_restore_access_but_a_new_one_can(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    first = await invite(alice, room_id, role="editor")
    assert (await redeem(bob, first["token"])).status_code == 200
    assert (await alice.delete(member_path(room_id, bob))).status_code == 204
    assert (await redeem(bob, first["token"])).json()["error"]["code"] == "invitation_invalid"
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 404

    fresh = await invite(alice, room_id, role="viewer")
    assert (await redeem(bob, fresh["token"])).status_code == 200
    assert (await bob.get(f"/rooms/{room_id}")).json()["role"] == "viewer"


# ---- epoch ledger -------------------------------------------------------------------------------


async def test_epoch_increments_exactly_once_per_access_change_and_never_otherwise(
    alice, bob, carol, sessionmaker
):
    ledger = []

    async def note(label, expected):
        actual = await epoch(sessionmaker, room_id)
        ledger.append((label, actual))
        assert actual == expected, (label, ledger)

    room_id = await new_room(alice)
    await note("room created", 1)
    inv = await invite(alice, room_id, role="editor")
    await note("invitation created", 1)
    await alice.patch(f"/rooms/{room_id}", json={"name": "Renamed"})
    await note("rename", 1)
    assert (await redeem(bob, inv["token"])).status_code == 200
    await note("redeem (new member)", 2)
    await redeem(bob, inv["token"])
    await note("idempotent redeem retry", 2)
    spare = await invite(alice, room_id)
    await redeem(bob, spare["token"])
    await note("existing member redeems another invite", 2)
    await alice.delete(f"/rooms/{room_id}/invitations/{spare['id']}")
    await note("invitation revoked", 2)
    await alice.patch(member_path(room_id, bob), json={"role": "editor"})
    await note("no-op role change", 2)
    await alice.patch(member_path(room_id, bob), json={"role": "viewer"})
    await note("role change", 3)
    await alice.delete(member_path(room_id, bob))
    await note("removal", 4)
    await alice.delete(member_path(room_id, bob))
    await note("repeat removal", 4)
    await alice.delete(member_path(room_id, alice))  # 409
    await alice.patch(member_path(room_id, alice), json={"role": "viewer"})  # 409
    await note("owner-protected attempts", 4)
    again = await invite(alice, room_id, role="viewer")
    await redeem(bob, again["token"])
    await note("rejoin by new invitation", 5)
    row = await rows(sessionmaker, "SELECT membership_epoch, version FROM rooms")
    # version = rename(1) + each epoch bump(4) = 5
    assert row[0]["membership_epoch"] == 5 and row[0]["version"] == 5


# ---- a failed transaction changes nothing --------------------------------------------------------


async def _boom(*_a, **_k):
    raise RuntimeError("simulated failure after the writes")


async def test_failed_role_change_leaves_everything_unchanged(
    alice, bob, sessionmaker, monkeypatch
):
    room_id = await setup_room(alice, bob, sessionmaker, "editor")
    monkeypatch.setattr(memberships_service, "bump_membership_epoch", _boom)
    response = await alice.patch(member_path(room_id, bob), json={"role": "viewer"})
    assert response.status_code == 500
    assert (await membership(sessionmaker, room_id, bob.id))["role"] == "editor"  # rolled back
    assert await epoch(sessionmaker, room_id) == 1


async def test_failed_removal_leaves_everything_unchanged(alice, bob, sessionmaker, monkeypatch):
    room_id = await setup_room(alice, bob, sessionmaker)
    monkeypatch.setattr(memberships_service, "bump_membership_epoch", _boom)
    assert (await alice.delete(member_path(room_id, bob))).status_code == 500
    row = await membership(sessionmaker, room_id, bob.id)
    assert (row["state"], row["removed_at"]) == ("active", None)
    assert await epoch(sessionmaker, room_id) == 1
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 200


async def test_failed_redemption_leaves_membership_invitation_and_epoch_unchanged(
    alice, bob, sessionmaker, monkeypatch
):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    monkeypatch.setattr(invitations_service, "bump_membership_epoch", _boom)
    assert (await redeem(bob, inv["token"])).status_code == 500
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=bob.id)
        == 0
    )
    [row] = await rows(sessionmaker, "SELECT used_at, used_by FROM invitations")
    assert (row["used_at"], row["used_by"]) == (None, None)
    assert await epoch(sessionmaker, room_id) == 1
    monkeypatch.undo()
    assert (await redeem(bob, inv["token"])).status_code == 200  # and it still works afterwards
