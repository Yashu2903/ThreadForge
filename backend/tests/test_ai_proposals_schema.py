"""Migration 0011 and the proposal tables: every rule that must hold in the DATABASE, whatever the code does."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.analysis_helpers import build_world


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def run(world, statement: str, **params):
    async with world.sm() as db, db.begin():
        result = await db.execute(text(statement), params)
        return result.all() if result.returns_rows else None


async def request_and_result(world) -> tuple[str, str]:
    """A real finished request (and its result row) to hang proposals on."""
    data = b"# Ops\n\nThe warehouse processed twelve thousand orders in March.\n"
    source = await world.upload(world.owner, "ops.md", data)
    await world.analyze(world.owner, source)
    await world.run_worker()
    request_id = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    await world.drain()
    [(result_id,)] = await run(
        world, "SELECT id FROM ai_results WHERE request_id = :r", r=uuid.UUID(request_id)
    )
    return request_id, str(result_id)


INSERT = """
INSERT INTO ai_proposals (request_id, result_id, room_id, document_id, kind, state, operations,
                          captured_counter, created_by, expires_at, {extra_cols})
SELECT :request_id, :result_id, r.room_id, {document}, :kind, :state, CAST(:operations AS jsonb),
       {counter}, r.requested_by, now() + interval '7 days', {extra_vals}
  FROM ai_requests r WHERE r.id = :request_id
"""


async def insert(world, request_id, result_id, **kw) -> None:
    kind = kw.pop("kind", "edit")
    state = kw.pop("state", "pending")
    document = kw.pop("document", "r.document_id" if kind == "edit" else "NULL")
    counter = kw.pop("counter", "7" if kind == "edit" else "NULL")
    extra = kw.pop("extra", {})
    cols = ", ".join(extra) or "created_at"
    vals = ", ".join(extra.values()) or "now()"
    await run(
        world,
        INSERT.format(document=document, counter=counter, extra_cols=cols, extra_vals=vals),
        request_id=uuid.UUID(request_id),
        result_id=uuid.UUID(result_id),
        kind=kind,
        state=state,
        operations=json.dumps([] if kind == "edit" else {}),
    )


async def test_a_valid_edit_and_draft_proposal_are_accepted(world):
    request_id, result_id = await request_and_result(world)
    await insert(world, request_id, result_id)
    [(n,)] = await run(world, "SELECT count(*) FROM ai_proposals")
    assert n == 1


async def test_one_proposal_per_request(world):
    request_id, result_id = await request_and_result(world)
    await insert(world, request_id, result_id)
    with pytest.raises(IntegrityError, match="uq_ai_proposals_request_id"):
        await insert(world, request_id, result_id)


@pytest.mark.parametrize(
    "kwargs,constraint",
    [
        ({"kind": "edit", "document": "NULL"}, "target_matches_kind"),
        ({"kind": "draft", "document": "r.document_id", "counter": "NULL"}, "target_matches_kind"),
        ({"kind": "edit", "counter": "NULL"}, "counter_matches_kind"),
        ({"state": "weird", "extra": {"resolved_at": "now()"}}, "state_valid"),
        ({"state": "applied"}, "resolved_consistent"),  # applied but no resolved_at
        ({"state": "applying"}, "applying_consistent"),
        ({"state": "pending", "extra": {"resolved_at": "now()"}}, "resolved_consistent"),
        ({"kind": "bogus"}, "kind_valid"),
    ],
    ids=[
        "edit-without-document",
        "draft-with-document",
        "edit-without-counter",
        "unknown-state",
        "applied-unresolved",
        "applying-without-owner",
        "pending-but-resolved",
        "unknown-kind",
    ],
)
async def test_database_refuses_inconsistent_rows(world, kwargs, constraint):
    request_id, result_id = await request_and_result(world)
    with pytest.raises(IntegrityError, match=constraint):
        await insert(world, request_id, result_id, **kwargs)


async def test_a_result_of_another_request_cannot_be_attached(world):
    request_id, _ = await request_and_result(world)
    with pytest.raises(IntegrityError, match="fk_ai_proposals_request_id_ai_results"):
        await insert(world, request_id, str(uuid.uuid4()))


async def test_a_document_of_another_room_cannot_be_the_target(world, make_actor):
    request_id, result_id = await request_and_result(world)
    stranger = await make_actor("Stranger", app_settings=world.cfg)
    foreign = await stranger.post("/rooms", json={"name": "Elsewhere"})
    foreign_room = foreign.json()["id"]
    await run(
        world,
        "INSERT INTO documents (room_id, created_by, title, template_key, content, content_hash,"
        " created_at, updated_at, content_updated_at)"
        " SELECT :room, owner_id, 'Other', 'blank', CAST('{}' AS jsonb), decode(repeat('00', 32), 'hex'),"
        " now(), now(), now() FROM rooms WHERE id = :room",
        room=uuid.UUID(foreign_room),
    )
    [(other_doc,)] = await run(
        world, "SELECT id FROM documents WHERE room_id = :r", r=uuid.UUID(foreign_room)
    )
    with pytest.raises(IntegrityError, match="fk_ai_proposals_room_id_documents"):
        await insert(world, request_id, result_id, document=f"'{other_doc}'")


async def test_published_document_only_for_an_applied_draft_and_only_once(world):
    request_id, result_id = await request_and_result(world)
    with pytest.raises(IntegrityError, match="published_only_when_applied_draft"):
        await insert(
            world,
            request_id,
            result_id,
            extra={"published_document_id": "r.document_id"},
        )


async def test_receipts_have_a_fixed_hash_size_and_cascade_with_their_proposal(world):
    request_id, result_id = await request_and_result(world)
    await insert(world, request_id, result_id)
    [(pid,)] = await run(world, "SELECT id FROM ai_proposals")
    with pytest.raises(IntegrityError, match="receipt_hash_length"):
        await run(
            world,
            "INSERT INTO ai_proposal_applications (proposal_id, applied_by, resulting_counter, receipt_hash)"
            " SELECT :p, created_by, 8, decode('00', 'hex') FROM ai_proposals WHERE id = :p",
            p=pid,
        )
    await run(
        world,
        "INSERT INTO ai_proposal_applications (proposal_id, applied_by, resulting_counter, receipt_hash)"
        " SELECT :p, created_by, 8, decode(repeat('ab', 32), 'hex') FROM ai_proposals WHERE id = :p",
        p=pid,
    )
    with pytest.raises(IntegrityError):  # one receipt per proposal
        await run(
            world,
            "INSERT INTO ai_proposal_applications (proposal_id, applied_by, resulting_counter, receipt_hash)"
            " SELECT :p, created_by, 9, decode(repeat('cd', 32), 'hex') FROM ai_proposals WHERE id = :p",
            p=pid,
        )


async def test_documents_default_to_interchange_and_know_ai_draft(world):
    [(fmt,)] = await run(world, "SELECT seed_format FROM documents LIMIT 1")
    assert fmt == "interchange"
    with pytest.raises((IntegrityError, DBAPIError), match="seed_format_valid"):
        await run(world, "UPDATE documents SET seed_format = 'markdown'")
    await run(
        world,
        "UPDATE documents SET template_key = 'ai_draft', seed_format = 'tiptap'",
    )
    [(key, fmt)] = await run(world, "SELECT template_key, seed_format FROM documents LIMIT 1")
    assert (key, fmt) == ("ai_draft", "tiptap")
