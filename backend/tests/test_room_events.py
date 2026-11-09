"""Atomic business mutations: change + room event + outbox row commit together (or not at all)."""

import ast
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.schemas.events import EVENT_SPECS
from app.services import invitations as invitations_service
from app.services import memberships as memberships_service
from app.services import messages as messages_service
from app.services import room_events
from app.services import rooms as rooms_service
from app.services.room_lock import LockedRoom, lock_room
from tests.helpers import add_member, execute, insert_messages, room_row, rows, scalar
from tests.test_invitations_api import invite, new_room, redeem
from tests.test_members_api import member_path
from tests.test_messages_edit_delete import delete, edit
from tests.test_messages_send import send
from tests.test_rooms_concurrency import contended

TABLES = [
    "rooms",
    "memberships",
    "invitations",
    "messages",
    "message_write_buckets",
    "room_events",
    "outbox",
]


async def events(sm, room_id):
    return await rows(sm, "SELECT * FROM room_events WHERE room_id = :r ORDER BY seq", r=room_id)


async def outbox_rows(sm):
    return await rows(sm, "SELECT * FROM outbox ORDER BY room_seq")


async def snapshot(sm):
    return {t: await rows(sm, f"SELECT * FROM {t} ORDER BY 1, 2") for t in TABLES}


async def event_count(sm):
    return await scalar(sm, "SELECT count(*) FROM room_events")


def assert_outbox_for(event, outbox):
    assert outbox["event_id"] == event["event_id"] and outbox["room_seq"] == event["seq"]
    assert outbox["state"] == "pending" and outbox["attempts"] == 0
    assert outbox["claim_token"] is None and outbox["claimed_until"] is None
    assert outbox["payload"] == {
        "schema_version": 1,
        "event_id": str(event["event_id"]),
        "room_id": str(event["room_id"]),
        "room_seq": event["seq"],
    }


# ---- each supported mutation creates change + event + outbox together ------------------------------


async def test_room_creation_makes_room_membership_and_seq_1_event_atomically(alice, sessionmaker):
    response = await alice.post("/rooms", json={"name": "New"})
    room_id = uuid.UUID(response.json()["id"])
    [event] = await events(sessionmaker, room_id)
    assert (event["seq"], event["type"], event["resource_type"]) == (1, "room_created", "room")
    assert (event["resource_id"], event["resource_version"], event["payload"]) == (
        room_id,
        None,
        {},
    )
    assert event["actor_id"] == alice.id and event["schema_version"] == 1
    assert (await room_row(sessionmaker, room_id))["event_seq"] == 1
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE room_id = :r", r=room_id)
        == 1
    )
    [outbox] = await outbox_rows(sessionmaker)
    assert_outbox_for(event, outbox)


async def test_rename_records_room_renamed(alice, sessionmaker):
    room_id = await new_room(alice)
    await alice.patch(f"/rooms/{room_id}", json={"name": "Renamed"})
    ev = (await events(sessionmaker, room_id))[-1]
    assert (ev["seq"], ev["type"], ev["resource_type"], ev["payload"]) == (
        2,
        "room_renamed",
        "room",
        {},
    )
    assert (ev["resource_id"], ev["resource_version"], ev["actor_id"]) == (
        uuid.UUID(room_id),
        None,
        alice.id,
    )
    assert (await room_row(sessionmaker, room_id))["name"] == "Renamed"
    assert len(await outbox_rows(sessionmaker)) == 2


async def test_redemption_records_member_joined_for_new_and_reactivated_members(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    first = await invite(alice, room_id, role="viewer")
    await redeem(bob, first["token"])
    await add_member(sessionmaker, room_id, bob.id, "viewer", "removed")  # removed, then rejoins
    second = await invite(alice, room_id, role="editor")
    await redeem(bob, second["token"])
    joined = [e for e in await events(sessionmaker, room_id) if e["type"] == "member_joined"]
    assert [e["payload"] for e in joined] == [
        {"user_id": str(bob.id), "role": "viewer"},
        {"user_id": str(bob.id), "role": "editor"},  # a reactivated membership emits member_joined
    ]
    for e in joined:
        assert (e["resource_type"], e["resource_id"], e["actor_id"]) == (
            "membership",
            bob.id,
            bob.id,
        )
        assert e["resource_version"] is None


async def test_role_change_records_member_role_changed(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await alice.patch(member_path(room_id, bob), json={"role": "viewer"})
    ev = (await events(sessionmaker, room_id))[-1]
    assert (ev["type"], ev["payload"], ev["actor_id"]) == (
        "member_role_changed",
        {"user_id": str(bob.id), "role": "viewer"},
        alice.id,
    )
    assert (ev["resource_type"], ev["resource_id"]) == ("membership", bob.id)


async def test_removal_records_member_removed(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await alice.delete(member_path(room_id, bob))
    ev = (await events(sessionmaker, room_id))[-1]
    assert (ev["type"], ev["payload"], ev["actor_id"]) == (
        "member_removed",
        {"user_id": str(bob.id)},
        alice.id,
    )


async def test_message_create_edit_delete_record_versioned_events(alice, sessionmaker):
    room_id = await new_room(alice)
    sent = (await send(alice, room_id, "hello")).json()
    await edit(alice, room_id, sent["id"], "hello again", 1)
    await delete(alice, room_id, sent["id"], 2)
    created, edited, deleted = (await events(sessionmaker, room_id))[1:]
    assert [(e["type"], e["resource_version"]) for e in (created, edited, deleted)] == [
        ("message_created", 1),
        ("message_edited", 2),
        ("message_deleted", 3),
    ]
    for e in (created, edited, deleted):
        assert (e["resource_type"], e["resource_id"], e["actor_id"], e["payload"]) == (
            "message",
            uuid.UUID(sent["id"]),
            alice.id,
            {},
        )
    assert [e["seq"] for e in await events(sessionmaker, room_id)] == [1, 2, 3, 4]


async def test_every_event_has_exactly_one_matching_pending_outbox_row(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    sent = (await send(bob, room_id, "x")).json()
    await edit(bob, room_id, sent["id"], "y", 1)
    await alice.patch(f"/rooms/{room_id}", json={"name": "N"})
    evs, outs = await events(sessionmaker, room_id), await outbox_rows(sessionmaker)
    assert len(evs) == len(outs) == 4
    for event, outbox in zip(evs, outs, strict=True):
        assert_outbox_for(event, outbox)


# ---- sequence allocation ---------------------------------------------------------------------------------


async def test_allocating_a_sequence_changes_neither_version_nor_updated_at_nor_epoch(
    alice, sessionmaker
):
    room_id = uuid.UUID(await new_room(alice))
    before = await room_row(sessionmaker, room_id)
    async with sessionmaker() as db, db.begin():
        locked = await lock_room(db, room_id)
        recorded = await room_events.record_room_event(
            db,
            locked,
            event_type="room_renamed",
            resource_type="room",
            resource_id=room_id,
            resource_version=None,
            actor_id=alice.id,
            payload={},
        )
    after = await room_row(sessionmaker, room_id)
    assert (after["event_seq"], recorded.room_seq) == (before["event_seq"] + 1, 2)
    for column in ("version", "updated_at", "membership_epoch", "name"):
        assert after[column] == before[column], column  # event_seq is its own counter


async def test_message_events_do_not_touch_the_membership_epoch_and_vice_versa(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    epoch = (await room_row(sessionmaker, room_id))["membership_epoch"]
    await send(alice, room_id, "x")
    assert (await room_row(sessionmaker, room_id))["membership_epoch"] == epoch
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await alice.patch(member_path(room_id, bob), json={"role": "viewer"})
    row = await room_row(sessionmaker, room_id)
    assert (row["membership_epoch"], row["event_seq"]) == (epoch + 1, 3)  # independent counters


async def test_separate_rooms_have_independent_sequences(alice, bob, sessionmaker):
    room_a, room_b = await new_room(alice, "A"), await new_room(bob, "B")
    await send(alice, room_a, "a1")
    await send(alice, room_a, "a2")
    await send(bob, room_b, "b1")
    assert [e["seq"] for e in await events(sessionmaker, room_a)] == [1, 2, 3]
    assert [e["seq"] for e in await events(sessionmaker, room_b)] == [1, 2]


async def test_concurrent_senders_get_distinct_contiguous_ordered_sequences(
    alice, make_actor, sessionmaker, monkeypatch
):
    room_id = await new_room(alice)
    senders = [alice]
    for i in range(7):
        editor = await make_actor(f"Writer{i}")
        await add_member(sessionmaker, room_id, editor.id, "editor")
        senders.append(editor)
    lock_order: list = []
    real = messages_service._authorize_member

    async def recording(db, room, actor):
        result = await real(db, room, actor)
        lock_order.append(actor)  # recorded while the room lock is held: this IS the commit order
        return result

    monkeypatch.setattr(messages_service, "_authorize_member", recording)
    results = await contended(
        sessionmaker, room_id, *[send(s, room_id, f"m{i}") for i, s in enumerate(senders)]
    )
    assert [r.status_code for r in results] == [201] * 8
    evs = await events(sessionmaker, room_id)
    assert [e["seq"] for e in evs] == list(range(1, 10))  # contiguous, no gaps, no duplicates
    created = evs[1:]
    assert [e["actor_id"] for e in created] == lock_order  # seq order == commit order
    # The guarantee is about SEQUENCE numbers: a later-committing transaction never has a lower
    # seq. created_at is the database host's wall clock (clock_timestamp()), informational only:
    # across backends it can step backwards on VMs (up to 471 ms observed on Docker Desktop), so
    # it is deliberately not asserted to be monotonic here.


async def test_concurrent_mixed_mutations_allocate_distinct_contiguous_sequences(
    alice, bob, carol, make_actor, sessionmaker
):
    room_id = await new_room(alice)
    third = await make_actor("Third")
    for actor in (bob, carol, third):
        await add_member(sessionmaker, room_id, actor.id, "editor")
    inv = await invite(alice, room_id)
    newcomer = await make_actor("Newcomer")
    results = await contended(
        sessionmaker,
        room_id,
        send(bob, room_id, "from bob"),
        send(carol, room_id, "from carol"),
        send(third, room_id, "from third"),
        alice.patch(f"/rooms/{room_id}", json={"name": "Renamed"}),
        alice.patch(member_path(room_id, bob), json={"role": "viewer"}),
        alice.delete(member_path(room_id, third)),
        redeem(newcomer, inv["token"]),
    )
    succeeded = [r for r in results if r.status_code in (200, 201, 204)]
    assert len(succeeded) >= 5  # (a send by the removed member may lose the race: 404)
    evs = await events(sessionmaker, room_id)
    assert [e["seq"] for e in evs] == list(range(1, len(succeeded) + 2))  # +1 for room_created
    assert (await room_row(sessionmaker, room_id))["event_seq"] == len(evs)
    assert len(await outbox_rows(sessionmaker)) == len(evs)


# ---- no-ops create no event --------------------------------------------------------------------------------


async def test_no_op_and_rejected_requests_create_no_event(
    alice, bob, carol, make_actor, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    outsider = await make_actor("Outsider")
    cmid = str(uuid.uuid4())
    sent = (await send(bob, room_id, "hello", cmid)).json()
    tomb = (await send(bob, room_id, "doomed")).json()
    await delete(alice, room_id, tomb["id"], 1)
    invitation = await invite(alice, room_id, role="viewer")
    await redeem(carol, invitation["token"])
    baseline = await event_count(sessionmaker)

    no_ops = {
        "matching duplicate send": send(bob, room_id, "hello", cmid),
        "unchanged edit": edit(bob, room_id, sent["id"], "hello", 1),
        "repeated delete": delete(alice, room_id, tomb["id"], 1),
        "unchanged role": alice.patch(member_path(room_id, carol), json={"role": "viewer"}),
        "redemption by an existing member": redeem(alice, (await invite(alice, room_id))["token"]),
        "retried redemption": redeem(carol, invitation["token"]),
        "same-name rename": alice.patch(f"/rooms/{room_id}", json={"name": "Room"}),
    }
    for label, call in no_ops.items():
        assert (await call).status_code in (200, 204), label
        assert await event_count(sessionmaker) == baseline, label

    await alice.delete(member_path(room_id, bob))  # a real removal: exactly one event
    assert await event_count(sessionmaker) == baseline + 1
    assert (await alice.delete(member_path(room_id, bob))).status_code == 204  # repeated removal
    assert await event_count(sessionmaker) == baseline + 1

    rejected = {
        "version conflict": edit(alice, room_id, sent["id"], "x", 99),
        "not the author": edit(alice, room_id, sent["id"], "x", 1),
        "outsider send": send(outsider, room_id, "x"),
        "invalid body": send(alice, room_id, ""),
        "tombstone edit": edit(alice, room_id, tomb["id"], "x", 2),
        "unknown message": delete(alice, room_id, str(uuid.uuid4()), 1),
        "owner removal": alice.delete(member_path(room_id, alice)),
        "bad invitation": redeem(outsider, "A" * 43),
    }
    for label, call in rejected.items():
        assert (await call).status_code >= 400, label
    assert await event_count(sessionmaker) == baseline + 1  # nothing rejected created an event


async def test_invitation_creation_and_revocation_emit_nothing(alice, sessionmaker):
    room_id = await new_room(alice)
    base = await event_count(sessionmaker)
    created = await invite(alice, room_id)
    await alice.delete(f"/rooms/{room_id}/invitations/{created['id']}")
    assert await event_count(sessionmaker) == base
    assert len(await outbox_rows(sessionmaker)) == base


async def test_a_duplicate_http_send_creates_exactly_one_message_event(alice, sessionmaker):
    import asyncio

    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    results = await asyncio.gather(*[send(alice, room_id, "double click", cmid) for _ in range(6)])
    assert sorted(r.status_code for r in results) == [200] * 5 + [201]
    created = [e for e in await events(sessionmaker, room_id) if e["type"] == "message_created"]
    assert len(created) == 1 and len(await outbox_rows(sessionmaker)) == 2  # room_created + one


# ---- failure rolls everything back --------------------------------------------------------------------------


async def _failing_helper(monkeypatch, module):
    real = module.record_room_event

    async def record_then_fail(*args, **kwargs):
        await real(*args, **kwargs)  # the sequence bump, event and outbox rows ARE written...
        raise RuntimeError("forced failure inside the recording helper")  # ...then it fails

    monkeypatch.setattr(module, "record_room_event", record_then_fail)


ROLLBACK_SCENARIOS = [
    "create_room",
    "rename",
    "joined",
    "role",
    "removal",
    "send",
    "edit",
    "delete",
]


@pytest.mark.parametrize("scenario", ROLLBACK_SCENARIOS)
async def test_a_failure_in_the_helper_rolls_back_change_sequence_event_and_outbox(
    scenario, alice, bob, make_actor, sessionmaker, monkeypatch
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    sent = (await send(bob, room_id, "existing message")).json()
    token = (await invite(alice, room_id))["token"]
    newcomer = await make_actor("Newcomer")
    before = await snapshot(sessionmaker)

    module, request = {
        "create_room": (rooms_service, lambda: alice.post("/rooms", json={"name": "doomed room"})),
        "rename": (
            rooms_service,
            lambda: alice.patch(f"/rooms/{room_id}", json={"name": "doomed"}),
        ),
        "joined": (invitations_service, lambda: redeem(newcomer, token)),
        "role": (
            memberships_service,
            lambda: alice.patch(member_path(room_id, bob), json={"role": "viewer"}),
        ),
        "removal": (memberships_service, lambda: alice.delete(member_path(room_id, bob))),
        "send": (messages_service, lambda: send(bob, room_id, "doomed message")),
        "edit": (messages_service, lambda: edit(bob, room_id, sent["id"], "doomed edit", 1)),
        "delete": (messages_service, lambda: delete(bob, room_id, sent["id"], 1)),
    }[scenario]
    await _failing_helper(monkeypatch, module)

    response = await request()
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    assert await snapshot(sessionmaker) == before  # EVERYTHING rolled back, incl. rooms.event_seq


# ---- the helper cannot be used without the room lock ------------------------------------------------------


async def test_record_room_event_requires_a_locked_room(alice, sessionmaker):
    room_id = uuid.UUID(await new_room(alice))
    kwargs = dict(
        event_type="room_renamed",
        resource_type="room",
        resource_id=room_id,
        resource_version=None,
        actor_id=alice.id,
        payload={},
    )
    async with sessionmaker() as db, db.begin():
        row = (await db.execute(text("SELECT * FROM rooms WHERE id = :r"), {"r": room_id})).one()
        for not_locked in (row, room_id, None, {"id": room_id}):
            with pytest.raises(TypeError, match="LockedRoom"):
                await room_events.record_room_event(db, not_locked, **kwargs)
    assert await event_count(sessionmaker) == 1  # only room_created; nothing was written


def test_a_locked_room_cannot_be_constructed_by_hand():
    with pytest.raises(TypeError, match="lock_room"):
        LockedRoom(row=object())
    with pytest.raises(TypeError, match="lock_room"):
        LockedRoom(object(), _proof=object())


@pytest.mark.parametrize(
    "bad",
    [
        {"event_type": "invitation_created"},  # unsupported type
        {"resource_type": "message"},  # wrong resource type for room_renamed
        {"resource_version": 3},  # room_renamed carries no version
        {"payload": {"message": "text must never be here"}},  # extra field
        {"resource_id": None},
    ],
)
async def test_event_fields_are_validated_before_anything_is_written(alice, sessionmaker, bad):
    room_id = uuid.UUID(await new_room(alice))
    kwargs = (
        dict(
            event_type="room_renamed",
            resource_type="room",
            resource_id=room_id,
            resource_version=None,
            actor_id=alice.id,
            payload={},
        )
        | bad
    )
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic's ValidationError is a ValueError
        async with sessionmaker() as db, db.begin():
            locked = await lock_room(db, room_id)
            await room_events.record_room_event(db, locked, **kwargs)
    assert (await room_row(sessionmaker, room_id))[
        "event_seq"
    ] == 1  # the sequence was not consumed


@pytest.mark.parametrize(
    ("event_type", "resource_type", "version", "payload"),
    [
        ("member_joined", "membership", None, {"user_id": str(uuid.uuid4())}),  # missing role
        ("member_joined", "membership", None, {"user_id": str(uuid.uuid4()), "role": "boss"}),
        ("member_removed", "membership", None, {"user_id": str(uuid.uuid4()), "role": "editor"}),
        ("message_created", "message", None, {}),  # versioned events need a version
        ("message_edited", "message", 0, {}),
        ("message_deleted", "message", 2, {"body": "deleted text"}),
        ("room_created", "room", None, {"anything": 1}),
    ],
)
def test_payload_schemas_are_strict(event_type, resource_type, version, payload):
    from app.schemas.events import validate_event_fields

    with pytest.raises(ValueError):  # noqa: PT011
        validate_event_fields(event_type, resource_type, uuid.uuid4(), version, payload)


def test_every_supported_event_type_has_a_spec_and_nothing_else_does():
    from app.models.room_event import EVENT_TYPES

    assert set(EVENT_SPECS) == set(EVENT_TYPES) and len(EVENT_TYPES) == 13


# ---- privacy ------------------------------------------------------------------------------------------------


async def test_events_and_outbox_never_contain_text_tokens_or_credentials(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    secret_text = "PRIVATE-MESSAGE-TEXT-7781"
    invitation = await invite(alice, room_id)
    await redeem(bob, invitation["token"])
    sent = (await send(bob, room_id, secret_text)).json()
    await edit(bob, room_id, sent["id"], secret_text + " edited", 1)
    await delete(bob, room_id, sent["id"], 2)
    dump = str(await rows(sessionmaker, "SELECT * FROM room_events")) + str(
        await outbox_rows(sessionmaker)
    )
    for forbidden in (secret_text, invitation["token"], "@", "tf_session", "csrf"):
        assert forbidden not in dump
    assert len(await events(sessionmaker, room_id)) == 5


# ---- completeness audit ---------------------------------------------------------------------------------------


SERVICE_FILES = ["rooms", "memberships", "invitations", "messages", "documents"]
# Functions that open a transaction and mutate: which events each must record.
EXPECTED_EVENTS = {
    "create_room": {"room_created"},
    "rename_room": {"room_renamed"},
    "change_role": {"member_role_changed"},
    "remove_member": {"member_removed"},
    "redeem_invitation": {"member_joined"},
    "send_message": {"message_created"},
    "edit_message": {"message_edited"},
    "delete_message": {"message_deleted"},
    # Part 6: set_active_document records its event through the _activate helper (see below)
    "create_document": {"document_created", "active_document_changed"},
    "rename_document": {"document_renamed"},
    "set_active_document": {"active_document_changed"},
    "save_content": {"document_content_updated"},
    # AI phase 1: the owner's room switch
    "set_room_ai_enabled": {"room_ai_settings_changed"},
}
# Helpers that record an event on behalf of the functions that call them.
HELPER_EVENTS = {"_activate(": {"active_document_changed"}}
# Deliberately event-less (invitations are not room events: spec section 5).
NO_EVENT_BY_DESIGN = {"create_invitation", "revoke_invitation"}


def _transaction_functions():
    found = {}
    for module in SERVICE_FILES:
        text = Path(f"app/services/{module}.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.AsyncFunctionDef):
                source = ast.get_source_segment(text, node)
                mutates = re.search(r"\b(insert|update|delete)\(", source) or any(
                    helper in source for helper in HELPER_EVENTS
                )
                if ".begin()" in source and mutates:
                    found[node.name] = source
    return found


def test_every_mutating_service_function_records_its_event():
    functions = _transaction_functions()
    # a NEW transaction-opening mutation must be classified here (event or deliberate exemption)
    assert set(functions) == set(EXPECTED_EVENTS) | NO_EVENT_BY_DESIGN, sorted(functions)
    for name, expected in EXPECTED_EVENTS.items():
        source = functions[name]
        produced = set(re.findall(r'event_type="(\w+)"', source))
        for helper, helper_events in HELPER_EVENTS.items():
            if helper in source:
                produced |= helper_events
        assert produced, f"{name} does not record a room event"
        assert "record_room_event(" in source or any(h in source for h in HELPER_EVENTS), name
        assert produced == expected, name
    for name in NO_EVENT_BY_DESIGN:
        assert "record_room_event(" not in functions[name]
    # the helper itself must record the event it is credited with
    helper_source = Path("app/services/documents.py").read_text(encoding="utf-8")
    assert 'event_type="active_document_changed"' in helper_source
    assert set().union(*EXPECTED_EVENTS.values()) == set(EVENT_SPECS)  # every type has a producer


def test_no_route_or_other_module_writes_events_or_the_outbox():
    allowed = {
        "app/services/room_events.py",
        "app/models/room_event.py",
        "app/models/outbox.py",
        "app/models/__init__.py",
    }
    for path in Path("app").rglob("*.py"):
        if (
            path.as_posix() in allowed
            or "outbox" in path.as_posix()
            or "workers" in path.as_posix()
        ):
            continue  # the dispatcher (a later checkpoint) legitimately updates outbox rows
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"\b(insert|update|delete)\((_EVENTS|_OUTBOX|RoomEvent)", source), path
        assert "INTO room_events" not in source and "UPDATE rooms SET event_seq" not in source, path


def test_the_application_never_updates_or_deletes_room_events():
    for path in Path("app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"(update|delete)\(\s*(_EVENTS|RoomEvent)", source), path
        assert "DELETE FROM room_events" not in source and "UPDATE room_events" not in source, path


async def test_existing_rooms_without_history_get_their_first_event_as_seq_1(alice, sessionmaker):
    """A room that predates the migration has event_seq 0 and no events; its first event is 1."""
    await execute(sessionmaker, "INSERT INTO rooms (name, owner_id) VALUES ('old', :u)", u=alice.id)
    old_room = await scalar(sessionmaker, "SELECT id FROM rooms WHERE name = 'old'")
    await add_member(sessionmaker, str(old_room), alice.id, "owner")
    assert (await room_row(sessionmaker, old_room))["event_seq"] == 0
    assert await events(sessionmaker, old_room) == []
    await send(alice, str(old_room), "first message after the migration")
    assert [(e["seq"], e["type"]) for e in await events(sessionmaker, old_room)] == [
        (1, "message_created")
    ]
    assert await insert_messages(
        sessionmaker, str(old_room), alice.id, 1
    )  # direct inserts: no events
    assert len(await events(sessionmaker, old_room)) == 1
