"""Database-level guarantees for documents, the active selection, events and the sync bootstrap.

The service layer is bypassed on purpose: these are the last line of defence."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.schemas.events import EVENT_SPECS, validate_event_fields
from tests.doc_helpers import create_doc, new_id, room_with_doc
from tests.helpers import execute, room_row, rows, scalar
from tests.test_invitations_api import new_room

HASH = "decode(repeat('ab', 32), 'hex')"


async def insert_doc(sm, room_id, user_id, *, title="T", template="blank", import_id=None, **over):
    values = {
        "title": title,
        "template_key": template,
        "content": '{"schema_version": 1, "blocks": []}',
        "import_id": import_id,
        "schema_version": 1,
        "content_version": 1,
        "metadata_version": 1,
        **over,
    }
    doc_id = str(uuid.uuid4())
    await execute(
        sm,
        "INSERT INTO documents (id, room_id, created_by, title, template_key, content, content_hash,"
        " import_id, schema_version, content_version, metadata_version, created_at, updated_at,"
        " content_updated_at)"
        f" VALUES (:id, :r, :u, :title, :template_key, CAST(:content AS jsonb), {HASH},"
        " :import_id, :schema_version, :content_version, :metadata_version, now(), now(), now())",
        id=doc_id,
        r=room_id,
        u=user_id,
        **values,
    )
    return doc_id


async def test_the_composite_foreign_key_rejects_a_document_from_another_room(
    alice, bob, sessionmaker
):
    room_a, _ = await room_with_doc(alice, sessionmaker)
    room_b, doc_b = await room_with_doc(bob, sessionmaker)
    with pytest.raises(IntegrityError, match="fk_rooms_active_document_documents"):
        await execute(
            sessionmaker,
            "UPDATE rooms SET active_document_id = :d WHERE id = :r",
            d=doc_b["id"],
            r=room_a,
        )
    own = await insert_doc(sessionmaker, room_a, alice.id)
    await execute(
        sessionmaker, "UPDATE rooms SET active_document_id = :d WHERE id = :r", d=own, r=room_a
    )
    assert str((await room_row(sessionmaker, room_a))["active_document_id"]) == own


async def test_a_null_active_document_is_not_checked_match_simple_semantics(alice, sessionmaker):
    """The foreign key is (id, active_document_id): with active_document_id NULL, PostgreSQL's
    default MATCH SIMPLE skips the check, so a room with no active document is valid."""
    room_id = await new_room(alice)
    assert (await room_row(sessionmaker, room_id))["active_document_id"] is None
    await execute(
        sessionmaker, "UPDATE rooms SET active_document_id = NULL WHERE id = :r", r=room_id
    )
    [fk] = await rows(
        sessionmaker,
        "SELECT confmatchtype FROM pg_constraint WHERE conname = 'fk_rooms_active_document_documents'",
    )
    assert fk["confmatchtype"] in ("s", b"s")  # 's' = MATCH SIMPLE


async def test_a_document_that_is_active_cannot_be_deleted(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    with pytest.raises(IntegrityError):
        await execute(sessionmaker, "DELETE FROM documents WHERE id = :d", d=doc["id"])


async def test_documents_and_rooms_cannot_be_deleted_while_referenced(alice, sessionmaker):
    room_id, _doc = await room_with_doc(alice, sessionmaker)
    with pytest.raises(IntegrityError):
        await execute(sessionmaker, "DELETE FROM rooms WHERE id = :r", r=room_id)


@pytest.mark.parametrize(
    "bad",
    [
        {"title": ""},
        {"title": " padded"},
        {"title": "x" * 201},
        {"title": "tab\there"},
        {"title": "del\x7fchar"},
        {"template_key": "nope"},
        {"template_key": "imported"},  # imported requires an import_id
        {"schema_version": 0},
        {"content": '"a string"'},
        {"content_version": 0},
        {"metadata_version": 0},
    ],
)
async def test_document_check_constraints(alice, sessionmaker, bad):
    room_id = await new_room(alice)
    # a 201-character title is stopped by the varchar(200) column before the CHECK is reached
    with pytest.raises(DBAPIError):
        await insert_doc(sessionmaker, room_id, alice.id, **bad)


async def test_a_short_content_hash_and_unpaired_request_fields_are_rejected(alice, sessionmaker):
    room_id = await new_room(alice)
    base = (
        "INSERT INTO documents (room_id, created_by, title, template_key, content, content_hash,"
        " {extra_cols} created_at, updated_at, content_updated_at)"
        " VALUES (:r, :u, 'T', 'blank', '{{}}', {hash}, {extra_vals} now(), now(), now())"
    )
    with pytest.raises(IntegrityError):
        await execute(
            sessionmaker,
            base.format(extra_cols="", extra_vals="", hash="decode('abcd', 'hex')"),
            r=room_id,
            u=alice.id,
        )
    with pytest.raises(IntegrityError):  # client_request_id without a fingerprint
        await execute(
            sessionmaker,
            base.format(
                extra_cols="client_request_id,", extra_vals="gen_random_uuid(),", hash=HASH
            ),
            r=room_id,
            u=alice.id,
        )


async def test_a_request_id_is_unique_per_room_and_creator_but_null_is_not(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    sql = (
        "INSERT INTO documents (room_id, created_by, title, template_key, content, content_hash,"
        " client_request_id, request_fingerprint, created_at, updated_at, content_updated_at)"
        f" VALUES (:r, :u, 'T', 'blank', '{{}}', {HASH}, :c, {HASH}, now(), now(), now())"
    )
    same = new_id()
    await execute(sessionmaker, sql, r=room_id, u=alice.id, c=same)
    with pytest.raises(IntegrityError, match="uq_documents_room_creator_request"):
        await execute(sessionmaker, sql, r=room_id, u=alice.id, c=same)
    await execute(sessionmaker, sql, r=room_id, u=alice.id, c=new_id())
    await insert_doc(sessionmaker, room_id, alice.id)  # NULL request ids never collide
    await insert_doc(sessionmaker, room_id, alice.id)


async def test_document_ids_are_unique_per_room_for_the_composite_keys(alice, sessionmaker):
    [constraint] = await rows(
        sessionmaker,
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
        " WHERE conname = 'uq_documents_room_id_id'",
    )
    assert constraint["d"] == "UNIQUE (room_id, id)"


async def test_the_listing_index_is_descending_on_created_at_and_id(sessionmaker):
    [index] = await rows(
        sessionmaker,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_documents_room_id_created_at_id'",
    )
    assert "(room_id, created_at DESC, id DESC)" in index["indexdef"]


async def test_the_new_event_types_and_resource_type_are_accepted_by_the_database(
    alice, sessionmaker
):
    room_id = await new_room(alice)
    for seq, event_type in enumerate(
        (
            "document_created",
            "document_renamed",
            "active_document_changed",
            "document_content_updated",
        ),
        start=2,
    ):
        await execute(
            sessionmaker,
            "UPDATE rooms SET event_seq = event_seq + 1, updated_at = updated_at WHERE id = :r",
            r=room_id,
        )
        await execute(
            sessionmaker,
            "INSERT INTO room_events (room_id, seq, type, resource_type, resource_id, actor_id,"
            " payload, created_at) VALUES (:r, :s, :t, 'document', gen_random_uuid(), :a, '{}',"
            " now())",
            r=room_id,
            s=seq,
            t=event_type,
            a=alice.id,
        )
    with pytest.raises(IntegrityError, match="ck_room_events_type_valid"):
        await execute(
            sessionmaker,
            "INSERT INTO room_events (room_id, seq, type, resource_type, actor_id, payload,"
            " created_at) VALUES (:r, 99, 'document_deleted', 'document', :a, '{}', now())",
            r=room_id,
            a=alice.id,
        )


def test_the_document_event_payload_schemas():
    did = uuid.uuid4()
    assert validate_event_fields(
        "document_created", "document", did, None, {"document_id": str(did)}
    ) == {"document_id": str(did)}
    assert validate_event_fields(
        "active_document_changed", "document", did, None,
        {"document_id": str(did), "selection_version": 3},
    ) == {"document_id": str(did), "selection_version": 3}  # fmt: skip
    assert validate_event_fields(
        "document_content_updated", "document", did, 7, {"document_id": str(did)}
    )
    bad_cases = [
        ("document_created", "document", did, None, {}),  # missing document_id
        ("document_created", "document", did, None, {"document_id": str(did), "title": "Secret"}),
        ("document_created", "message", did, None, {"document_id": str(did)}),
        ("document_created", "document", did, 1, {"document_id": str(did)}),  # must be unversioned
        ("document_content_updated", "document", did, None, {"document_id": str(did)}),
        ("active_document_changed", "document", did, None, {"document_id": str(did)}),
        ("active_document_changed", "document", did, None,
         {"document_id": str(did), "selection_version": "3"}),
        ("document_renamed", "document", None, None, {"document_id": str(did)}),
    ]  # fmt: skip
    for case in bad_cases:
        with pytest.raises(ValueError):
            validate_event_fields(*case)
    assert {"document_created", "document_renamed", "active_document_changed",
            "document_content_updated"} <= set(EVENT_SPECS)  # fmt: skip


# ---- the Part 5 bootstrap and the room history carry the new state ---------------------------------------------------------


async def test_sync_bootstraps_the_active_document_in_the_same_snapshot(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    empty = (await alice.get(f"/rooms/{room_id}/sync")).json()
    assert (empty["room"]["active_document_id"], empty["room"]["active_document_version"]) == (
        None, 0,
    )  # fmt: skip
    doc = (await create_doc(alice, room_id)).json()
    synced = (await alice.get(f"/rooms/{room_id}/sync")).json()
    assert (synced["room"]["active_document_id"], synced["room"]["active_document_version"]) == (
        doc["id"], 1,
    )  # fmt: skip
    assert synced["through_seq"] == 3  # room_created + document_created + active_document_changed


async def test_document_events_are_in_the_history_endpoint_and_have_no_content(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, template="meeting_notes")
    response = await alice.get(f"/rooms/{room_id}/events?after_seq=1")
    items = response.json()["items"]
    assert [i["event_type"] for i in items] == ["document_created", "active_document_changed"]
    assert all(i["resource_type"] == "document" and i["resource_id"] == doc["id"] for i in items)
    assert "Agenda" not in response.text and "Plan" not in str(items)


async def test_migration_0005_downgrade_refuses_when_document_events_exist(alice, sessionmaker):
    """Events are append-only and gapless: the downgrade will not delete them to make it fit."""
    import asyncio

    from alembic import command

    from tests.db_utils import alembic_config

    await room_with_doc(alice, sessionmaker)
    assert await scalar(sessionmaker, "SELECT count(*) FROM room_events WHERE type LIKE 'doc%'") > 0
    # run in a thread: Alembic's env runs its own event loop
    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        await asyncio.to_thread(command.downgrade, alembic_config(), "0004")
    assert await scalar(sessionmaker, "SELECT count(*) FROM documents") == 1
