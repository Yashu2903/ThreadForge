"""The dependency pre-check is a fast rejection, NEVER the authority for a mutation.

Each mutating service re-checks the actor's role under the room lock. These tests prove it two
ways: calling services directly with no dependency in front, and simulating a stale pre-check
(the dependency says "owner" but the database says otherwise).
"""

import pytest

from app.errors import AppError
from app.services import invitations, memberships, rooms
from tests.factories import make_settings
from tests.helpers import add_member, execute, room_row, scalar
from tests.test_invitations_api import epoch, invite, new_room, redeem
from tests.test_members_api import member_path

SETTINGS = make_settings()


async def room_with(sessionmaker, alice, others):
    room_id = await new_room(alice)
    for actor, role, state in others:
        await add_member(sessionmaker, room_id, actor.id, role, state)
    return room_id


def rejects(code):
    return pytest.raises(AppError, match=code)


async def test_services_enforce_roles_without_any_dependency_in_front(
    alice, bob, carol, make_actor, sessionmaker
):
    import uuid

    editor, viewer, removed = bob, carol, await make_actor("Dave")
    room_id = await room_with(
        sessionmaker,
        alice,
        [
            (editor, "editor", "active"),
            (viewer, "viewer", "active"),
            (removed, "editor", "removed"),
        ],
    )
    rid = uuid.UUID(room_id)
    stranger = await make_actor("Stranger")

    for actor, code in (
        (editor, "insufficient_role"),
        (viewer, "insufficient_role"),
        (removed, "room_not_found"),
        (stranger, "room_not_found"),
    ):
        with rejects(code):
            await rooms.rename_room(sessionmaker, actor.id, rid, "hijacked")
        with rejects(code):
            await invitations.create_invitation(sessionmaker, SETTINGS, actor.id, rid, "editor", 24)
        with rejects(code):
            await memberships.change_role(sessionmaker, actor.id, rid, viewer.id, "editor")
        with rejects(code):
            await memberships.remove_member(sessionmaker, actor.id, rid, viewer.id)
        with rejects(code):
            await invitations.revoke_invitation(sessionmaker, actor.id, rid, uuid.uuid4())

    assert (await room_row(sessionmaker, room_id))["name"] == "Room"
    assert await epoch(sessionmaker, room_id) == 1
    assert await scalar(sessionmaker, "SELECT count(*) FROM invitations") == 0


async def test_a_stale_dependency_precheck_cannot_authorize_a_mutation(
    alice, bob, sessionmaker, monkeypatch
):
    """bob was removed, but the (stale) pre-check still believes he is the owner. The routes get
    past their dependencies and the service, under the lock, rejects every one of them."""
    room_id = await room_with(sessionmaker, alice, [(bob, "editor", "removed")])
    inv = await invite(alice, room_id)

    async def stale_owner(*_args, **_kwargs):
        return "owner"

    monkeypatch.setattr("app.dependencies.rooms.get_active_role", stale_owner)
    attempts = [
        bob.patch(f"/rooms/{room_id}", json={"name": "hijacked"}),
        bob.post(f"/rooms/{room_id}/invitations", json={}),
        bob.delete(f"/rooms/{room_id}/invitations/{inv['id']}"),
        bob.patch(member_path(room_id, alice), json={"role": "viewer"}),
        bob.delete(member_path(room_id, alice)),
    ]
    for call in attempts:
        response = await call
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert (await room_row(sessionmaker, room_id))["name"] == "Room"
    assert await epoch(sessionmaker, room_id) == 1


async def test_a_stale_member_precheck_for_a_viewer_posing_as_owner_gets_insufficient_role(
    alice, bob, sessionmaker, monkeypatch
):
    room_id = await room_with(sessionmaker, alice, [(bob, "viewer", "active")])

    async def stale_owner(*_args, **_kwargs):
        return "owner"

    monkeypatch.setattr("app.dependencies.rooms.get_active_role", stale_owner)
    response = await bob.patch(member_path(room_id, alice), json={"role": "viewer"})
    assert (response.status_code, response.json()["error"]["code"]) == (403, "insufficient_role")
    assert await epoch(sessionmaker, room_id) == 1


async def test_identity_comes_from_the_session_never_from_the_request(alice, bob, sessionmaker):
    """No endpoint accepts an actor id; extra fields are rejected, and headers are ignored."""
    room_id = await room_with(sessionmaker, alice, [])
    response = await bob.patch(
        f"/rooms/{room_id}",
        json={"name": "x"},
        headers={"X-User-Id": str(alice.id), "X-Actor-Id": str(alice.id)},
    )
    assert response.status_code == 404  # bob is still just bob
    response = await bob.patch(f"/rooms/{room_id}", json={"name": "x", "actor_id": str(alice.id)})
    assert response.status_code == 404  # nonmember: authorization before body validation


async def test_disabled_account_loses_room_access_over_http(alice, bob, sessionmaker):
    room_id = await room_with(sessionmaker, alice, [(bob, "editor", "active")])
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 200
    await bob.csrf_token()  # fetch while still allowed; /auth/csrf itself 401s once disabled
    await execute(sessionmaker, "UPDATE users SET state = 'disabled' WHERE id = :u", u=bob.id)
    for call in (bob.get(f"/rooms/{room_id}"), bob.get("/rooms")):
        assert (await call).status_code == 401  # authentication fails before authorization
    assert (await redeem(bob, "A" * 43)).status_code == 401
