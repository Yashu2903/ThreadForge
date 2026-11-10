"""Phase 2, checkpoints 3 and 4 (Python side): everything that happens to a proposal AFTER it exists.

* the internal routes the collaboration service uses to apply one (fenced, compare-and-set, receipts);
* the public routes: apply (with the collaboration service replaced by a recorder), reject, regenerate, publish;
* expiry, retention, events, and the permission flags the review card shows.

The apply itself (Yjs, hashes, the quiet guard, crash recovery) is tested against the real service in
collab/test/apply.test.ts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import httpx2
import pytest

from tests.analysis_helpers import (
    build_world,
    draft_reply,
    edit_reply,
    new_block,
    op,
    para,
    run_,
)
from tests.helpers import add_member, rows, scalar
from tests.helpers import execute as sql
from tests.test_ai_proposals_generation import (  # noqa: F401  (fixtures and helpers)
    OPS,
    collab_for,
    go,
    proposals,
    ready,
    send_edit,
)

SECRET = {}  # filled per world


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_proposals_enabled=True
    )


def internal(world, method: str, path: str, token: int | None = None, **kwargs):
    headers = {"X-Internal-Secret": world.cfg.internal_api_secret.get_secret_value()}
    if token is not None:
        headers["X-Collab-Lease"] = str(token)
    return world.owner.client.request(method, f"/internal/collab{path}", headers=headers, **kwargs)


async def lease(world) -> int:
    r = await internal(world, "POST", "/lease/acquire", json={"holder_id": "holder-aaaaaaaa"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def make_proposal(world, kind="edit", **kw):
    fc = collab_for(world)
    if kind == "edit":
        world.stub.edit_script = [
            edit_reply(op("replace_block_text", block="B3", new_inline=[run_("New budget.")]))
        ]
        rid, out = await go(world, fc, 2)
    else:
        world.stub.edit_script = []
        source = await world.upload(world.owner, "ops.md", OPS)
        world.stub.draft_script = [
            draft_reply(
                "Warehouse report",
                new_block("heading", level=1, inline=[run_("Warehouse report")]),
                para("March was busy.", "P1"),
            )
        ]
        r = await world.owner.post(
            f"/rooms/{world.room_id}/ai-requests",
            json={
                "client_request_id": str(uuid.uuid4()),
                "instruction": "Draft a report on the warehouse.",
                "operation": "draft",
                "task_hint": "ask",
                "source_version_ids": [world.version_of(source)[1]],
            },
        )
        assert r.status_code == 201, r.text
        out = await world.drain()
    assert any(o.startswith("request:published") for o in out), out
    got = await proposals(world)
    return got[-1]


async def events_of_type(world, kind: str):
    return await rows(
        world.sm,
        "SELECT payload FROM room_events WHERE type = :t ORDER BY seq",
        t=kind,
    )


def begin(world, token, proposal, user=None, document=None):
    return internal(
        world,
        "POST",
        f"/proposals/{proposal['id']}/begin-apply",
        token,
        json={
            "user_id": str(user or world.owner.id),
            "document_id": str(document or proposal["document_id"]),
        },
    )


# ---- internal routes: the collaboration service's side --------------------------------------------------------


async def test_begin_apply_takes_a_pending_proposal_once_and_announces_it(world):
    prop = await make_proposal(world)
    token = await lease(world)
    r = await begin(world, token, prop)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_applied"] is False
    assert body["operations"] == prop["operations"] and body["base_hashes"] == prop["base_hashes"]
    assert body["scope"]["whole_ids"] and body["captured_counter"] == prop["captured_counter"]
    [row] = await rows(world.sm, "SELECT state, applying_by FROM ai_proposals")
    assert (row["state"], str(row["applying_by"])) == ("applying", str(world.owner.id))
    states = [e["payload"]["state"] for e in await events_of_type(world, "ai_proposal_updated")]
    assert states == ["pending", "applying"]


async def test_two_competing_begins_one_wins_and_the_same_user_may_resume(world, make_actor):
    prop = await make_proposal(world)
    editor = await make_actor("Editor", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, editor.id, "editor")
    token = await lease(world)
    first, second = await asyncio.gather(
        begin(world, token, prop), begin(world, token, prop, user=editor.id)
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "proposal_applying"
    winner = world.owner.id if first.status_code == 200 else editor.id
    assert (
        await begin(world, token, prop, user=winner)
    ).status_code == 200  # resuming one's own apply


async def test_begin_apply_checks_fencing_document_and_write_access(world, make_actor):
    prop = await make_proposal(world)
    token = await lease(world)
    stale = await begin(world, token + 5, prop)
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "stale_lease")
    other_doc = await begin(world, token, prop, document=uuid.uuid4())
    assert (other_doc.status_code, other_doc.json()["error"]["code"]) == (
        404,
        "ai_proposal_not_found",
    )
    viewer = await make_actor("Viewer", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, viewer.id, "viewer")
    outsider = await make_actor("Outsider", app_settings=world.cfg)
    for user in (viewer.id, outsider.id):
        r = await begin(world, token, prop, user=user)
        assert r.status_code in (403, 404), r.text
    await sql(world.sm, "UPDATE documents SET collab_state = 'needs_repair'")
    r = await begin(world, token, prop)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "ai_write_access_required")
    [row] = await rows(world.sm, "SELECT state FROM ai_proposals")
    assert row["state"] == "pending"


async def test_begin_apply_refuses_expired_and_final_states(world):
    prop = await make_proposal(world)
    token = await lease(world)
    await sql(
        world.sm,
        "UPDATE ai_proposals SET expires_at = now() - interval '1 second', created_at = now() - interval '1 day'",
    )
    r = await begin(world, token, prop)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "proposal_expired")
    assert (await rows(world.sm, "SELECT state FROM ai_proposals"))[0]["state"] == "expired"
    for state in ("rejected", "superseded", "conflicted"):
        await sql(
            world.sm,
            "UPDATE ai_proposals SET state = :s, resolved_at = now(), expires_at = now() + interval '1 day'",
            s=state,
        )
        r = await begin(world, token, prop)
        assert r.json()["error"]["code"] == f"proposal_{state}"


async def test_finish_release_and_conflict_transitions(world):
    prop = await make_proposal(world)
    token = await lease(world)
    receipt = hashlib.sha256(b"x").hexdigest()
    finish = lambda: internal(  # noqa: E731
        world,
        "POST",
        f"/proposals/{prop['id']}/finish-apply",
        token,
        json={"user_id": str(world.owner.id), "resulting_counter": 9, "receipt_hash": receipt},
    )
    early = await finish()
    assert (early.status_code, early.json()["error"]["code"]) == (409, "proposal_pending")
    await begin(world, token, prop)
    release = await internal(
        world,
        "POST",
        f"/proposals/{prop['id']}/release-apply",
        token,
        json={"reason": "document_busy"},
    )
    assert release.status_code == 204
    assert (await rows(world.sm, "SELECT state, applying_by FROM ai_proposals"))[0][
        "state"
    ] == "pending"
    await begin(world, token, prop)
    conflict = await internal(
        world,
        "POST",
        f"/proposals/{prop['id']}/conflicted",
        token,
        json={"user_id": str(world.owner.id), "code": "block_changed"},
    )
    assert conflict.status_code == 204
    [row] = await rows(world.sm, "SELECT state, resolution_code, resolved_by FROM ai_proposals")
    assert (row["state"], row["resolution_code"], str(row["resolved_by"])) == (
        "conflicted",
        "block_changed",
        str(world.owner.id),
    )
    # a fresh proposal: begin, finish (twice: idempotent)
    await sql(
        world.sm,
        "UPDATE ai_proposals SET state='pending', resolved_at=NULL, resolved_by=NULL, resolution_code=NULL",
    )
    await begin(world, token, prop)
    assert (await finish()).json() == {"state": "applied"}
    assert (await finish()).json() == {"state": "applied"}
    [app_row] = await rows(
        world.sm, "SELECT resulting_counter, applied_by FROM ai_proposal_applications"
    )
    assert (app_row["resulting_counter"], str(app_row["applied_by"])) == (9, str(world.owner.id))
    states = [e["payload"]["state"] for e in await events_of_type(world, "ai_proposal_updated")]
    assert states == [
        "pending",
        "applying",
        "pending",
        "applying",
        "conflicted",
        "applying",
        "applied",
    ]


async def test_the_store_records_the_receipt_in_its_own_transaction(world):
    prop = await make_proposal(world)
    token = await lease(world)
    fc_doc = prop["document_id"]
    await begin(world, token, prop)
    seed = await world.owner.client.put(
        f"/internal/collab/documents/{fc_doc}/state",
        content=b"seed",
        headers={
            "X-Collab-Lease": str(token),
            "X-Update-Counter": "0",
            "X-Insert-If-Absent": "true",
            "X-Internal-Secret": world.cfg.internal_api_secret.get_secret_value(),
        },
    )
    assert seed.status_code == 204, seed.text
    receipt_headers = {
        "X-Collab-Lease": str(token),
        "X-Internal-Secret": world.cfg.internal_api_secret.get_secret_value(),
        "X-Update-Counter": "1",
        "X-Proposal-Id": str(prop["id"]),
        "X-Applied-By": str(world.owner.id),
        "X-Receipt-Hash": hashlib.sha256(b"r").hexdigest(),
    }
    bad = await world.owner.client.put(
        f"/internal/collab/documents/{fc_doc}/state",
        content=b"s1",
        headers={**receipt_headers, "X-Receipt-Hash": "abcd"},
    )
    assert bad.status_code == 422
    stale = await world.owner.client.put(
        f"/internal/collab/documents/{fc_doc}/state",
        content=b"s1",
        headers={**receipt_headers, "X-Collab-Lease": str(token + 3)},
    )
    assert stale.status_code == 409
    assert await scalar(world.sm, "SELECT count(*) FROM ai_proposal_applications") == 0
    ok = await world.owner.client.put(
        f"/internal/collab/documents/{fc_doc}/state", content=b"s1", headers=receipt_headers
    )
    assert ok.status_code == 204, ok.text
    [r] = await rows(world.sm, "SELECT resulting_counter, applied_by FROM ai_proposal_applications")
    assert (r["resulting_counter"], str(r["applied_by"])) == (1, str(world.owner.id))
    # a receipt for a proposal that is not being applied records nothing, and the state is stored anyway
    await sql(world.sm, "DELETE FROM ai_proposal_applications")
    await sql(
        world.sm,
        "UPDATE ai_proposals SET state='rejected', resolved_at=now(), applying_by=NULL, applying_since=NULL",
    )
    again = await world.owner.client.put(
        f"/internal/collab/documents/{fc_doc}/state",
        content=b"s2",
        headers={**receipt_headers, "X-Update-Counter": "2"},
    )
    assert again.status_code == 204
    assert await scalar(world.sm, "SELECT count(*) FROM ai_proposal_applications") == 0


async def test_stuck_proposals_are_listed_by_age(world):
    prop = await make_proposal(world)
    token = await lease(world)
    await begin(world, token, prop)
    fresh = await internal(world, "GET", "/proposals/stuck?older_than_seconds=60")
    assert fresh.json() == {"proposals": []}
    await sql(world.sm, "UPDATE ai_proposals SET applying_since = now() - interval '5 minutes'")
    old = (await internal(world, "GET", "/proposals/stuck?older_than_seconds=60")).json()[
        "proposals"
    ]
    assert [p["proposal_id"] for p in old] == [str(prop["id"])]


# ---- the public routes ---------------------------------------------------------------------------------------------


def with_collab(world, status: int, body: dict, actors=None) -> list[dict]:
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "path": request.url.path,
                "json": json.loads(request.content),
                "secret": request.headers.get("x-internal-secret"),
            }
        )
        return httpx2.Response(status, json=body)

    for actor in actors or [world.owner]:
        actor.client._transport.app.state.collab_transport = httpx2.MockTransport(handler)
    return seen


async def test_apply_asks_the_collaboration_service_and_reports_synced_and_durable(world):
    prop = await make_proposal(world)
    seen = with_collab(
        world,
        200,
        {
            "state": "applied",
            "already": False,
            "synced": True,
            "durable": True,
            "counter": 4,
            "connected_users": 2,
        },
    )
    r = await world.owner.post(f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/apply", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "state": "applied",
        "already": False,
        "synced": True,
        "durable": True,
        "counter": 4,
        "connected_users": 2,
    }
    assert seen == [
        {
            "path": "/internal/ai/apply-proposal",
            "json": {
                "proposal_id": str(prop["id"]),
                "document_id": str(prop["document_id"]),
                "user_id": str(world.owner.id),
            },
            "secret": world.cfg.internal_api_secret.get_secret_value(),
        }
    ]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (409, {"error": "conflicted", "code": "block_changed"}, (409, "proposal_conflicted")),
        (409, {"error": "document_busy", "retryable": True}, (409, "document_busy")),
        (409, {"error": "proposal_rejected", "state": "rejected"}, (409, "proposal_rejected")),
        (409, {"error": "stale_lease"}, (503, "collab_unavailable")),
        (403, {"error": "ai_write_access_required"}, (403, "ai_write_access_required")),
        (503, {"error": "unavailable"}, (503, "collab_unavailable")),
    ],
)
async def test_apply_translates_the_services_refusals(world, status, body, expected):
    prop = await make_proposal(world)
    with_collab(world, status, body)
    r = await world.owner.post(f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/apply", json={})
    assert (r.status_code, r.json()["error"]["code"]) == expected


async def test_apply_is_for_people_who_may_see_and_write(world, make_actor):
    prop = await make_proposal(world)
    seen = with_collab(
        world,
        200,
        {
            "state": "applied",
            "already": False,
            "synced": True,
            "durable": True,
            "counter": 1,
            "connected_users": 0,
        },
    )
    viewer = await make_actor("Viewer", app_settings=world.cfg)
    outsider = await make_actor("Outsider", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, viewer.id, "viewer")
    url = f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/apply"
    got = await viewer.post(url, json={})
    assert (got.status_code, got.json()["error"]["code"]) == (403, "insufficient_role")
    got = await outsider.post(url, json={})
    assert (got.status_code, got.json()["error"]["code"]) == (404, "room_not_found")
    anonymous = await world.owner.client.post(url, json={})  # no CSRF token
    assert anonymous.status_code == 403
    assert seen == []
    # an unknown proposal and a draft are refused
    missing = await world.owner.post(
        f"/rooms/{world.room_id}/ai-proposals/{uuid.uuid4()}/apply", json={}
    )
    assert missing.status_code == 404


async def test_reject_changes_nothing_else_and_only_a_pending_one_can_be_rejected(
    world, make_actor
):
    prop = await make_proposal(world)
    editor = await make_actor("Editor", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, editor.id, "editor")
    url = f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/reject"
    viewer = await make_actor("Viewer", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, viewer.id, "viewer")
    assert (await viewer.post(url, json={})).status_code == 403
    r = await editor.post(
        url, json={}
    )  # an editor with write access may reject someone else's proposal
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "rejected" and r.json()["can_apply"] is False
    again = await world.owner.post(url, json={})
    assert again.status_code == 200 and again.json()["state"] == "rejected"
    states = [e["payload"]["state"] for e in await events_of_type(world, "ai_proposal_updated")]
    assert states == ["pending", "rejected"]  # the second reject announced nothing
    assert await scalar(world.sm, "SELECT count(*) FROM documents") == 1
    # not while being applied, and not after it was applied
    other = await make_proposal(world)
    await sql(
        world.sm,
        "UPDATE ai_proposals SET state='applying', applying_by=created_by, applying_since=now() WHERE id = :i",
        i=other["id"],
    )
    got = await world.owner.post(
        f"/rooms/{world.room_id}/ai-proposals/{other['id']}/reject", json={}
    )
    assert (got.status_code, got.json()["error"]["code"]) == (409, "proposal_applying")


async def test_the_card_gets_exactly_the_actions_the_person_may_take(world, make_actor):
    prop = await make_proposal(world)
    editor = await make_actor("Editor", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, editor.id, "editor")
    url = f"/rooms/{world.room_id}/ai-proposals/{prop['id']}"
    owner = (await world.owner.get(url)).json()
    assert (
        owner["can_apply"],
        owner["can_reject"],
        owner["can_regenerate"],
        owner["can_publish"],
    ) == (True, True, False, False)
    assert (await editor.get(url)).json()["can_apply"] is True
    await sql(world.sm, "UPDATE documents SET collab_state = 'needs_repair'")
    again = (
        await world.owner.get(url)
    ).json()  # the requester keeps seeing it but cannot apply to a locked document
    assert (again["can_apply"], again["can_reject"]) == (False, True)
    await sql(world.sm, "UPDATE documents SET collab_state = 'ready'")
    await sql(
        world.sm,
        "UPDATE ai_proposals SET expires_at = now() - interval '1 second', created_at = now() - interval '1 day'",
    )
    expired = (await world.owner.get(url)).json()
    assert expired["state"] == "expired" and not any(
        expired[k] for k in ("can_apply", "can_reject", "can_regenerate", "can_publish")
    )


async def test_regenerate_asks_again_from_the_current_text_and_supersedes_the_old_preview(world):
    fc = collab_for(world)
    world.stub.edit_script = [
        edit_reply(op("replace_block_text", block="B3", new_inline=[run_("First try.")]))
    ]
    rid, out = await go(world, fc, 2, instruction="Update the budget line.")
    [old] = await proposals(world)
    # the proposal conflicted when it was applied
    await sql(
        world.sm,
        "UPDATE ai_proposals SET state='conflicted', resolved_at=now(), resolution_code='block_changed'",
    )
    fc.edit(2, "The budget is now fifty thousand dollars.")  # someone changed the block meanwhile
    world.stub.edit_script = [
        edit_reply(op("replace_block_text", block="B3", new_inline=[run_("Second try.")]))
    ]
    r = await world.owner.post(
        f"/rooms/{world.room_id}/ai-proposals/{old['id']}/regenerate",
        json={"client_request_id": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "queued" and r.json()["operation"] == "edit"
    assert (await rows(world.sm, "SELECT state FROM ai_proposals WHERE id = :i", i=old["id"]))[0][
        "state"
    ] == "superseded"
    # the old preview is still readable
    assert (await world.owner.get(f"/rooms/{world.room_id}/ai-proposals/{old['id']}")).json()[
        "preview"
    ]["changes"]
    outcomes = await world.drain(collab=fc.transport())
    assert outcomes == ["request:published"], outcomes
    new = (await proposals(world))[-1]
    assert str(new["regenerated_from"]) == str(old["id"]) and new["state"] == "pending"
    # it was based on the CURRENT text (the base hash is the edited block's hash), without a client hash
    assert new["base_hashes"][fc.block_ids(2)[0]] == fc.blocks[2].hash
    assert new["operations"][0]["new_inline"][0]["text"] == "Second try."
    # only a conflicted proposal can be regenerated
    again = await world.owner.post(
        f"/rooms/{world.room_id}/ai-proposals/{new['id']}/regenerate",
        json={"client_request_id": str(uuid.uuid4())},
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "proposal_pending")


async def test_publishing_a_draft_is_owner_only_idempotent_and_atomic(world, make_actor):
    prop = await make_proposal(world, kind="draft")
    editor = await make_actor("Editor", app_settings=world.cfg)
    await add_member(world.sm, world.room_id, editor.id, "editor")
    url = f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/publish"
    before_docs = await scalar(world.sm, "SELECT count(*) FROM documents")
    denied = await editor.post(url, json={"client_request_id": str(uuid.uuid4())})
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "insufficient_role")
    assert await scalar(world.sm, "SELECT count(*) FROM documents") == before_docs
    # the editor's own draft waits for the owner: the flags say so
    got = (await world.owner.get(f"/rooms/{world.room_id}/ai-proposals/{prop['id']}")).json()
    assert got["can_publish"] is True and got["can_apply"] is False

    key = str(uuid.uuid4())
    seq_before = await scalar(world.sm, "SELECT event_seq FROM rooms")
    first = await world.owner.post(url, json={"client_request_id": key})
    assert first.status_code == 200, first.text
    doc_id = first.json()["document_id"]
    assert first.json() == {
        "document_id": doc_id,
        "title": "Warehouse report",
        "created": True,
        "is_active": True,
    }
    [doc] = await rows(
        world.sm,
        "SELECT template_key, seed_format, content, collab_state FROM documents WHERE id = :i",
        i=uuid.UUID(doc_id),
    )
    assert (doc["template_key"], doc["seed_format"], doc["collab_state"]) == (
        "ai_draft",
        "tiptap",
        "legacy",
    )
    assert [n["type"] for n in doc["content"]["content"]] == [
        "heading",
        "paragraph",
        "heading",  # the server-built "Sources" section: the cited file
        "bulletList",
    ]
    [row] = await rows(
        world.sm,
        "SELECT state, resolution_code, published_document_id FROM ai_proposals WHERE id = :i",
        i=prop["id"],
    )
    assert (row["state"], row["resolution_code"], str(row["published_document_id"])) == (
        "applied",
        "published",
        doc_id,
    )
    assert str(await scalar(world.sm, "SELECT active_document_id FROM rooms")) == doc_id
    # one transaction: document_created, active_document_changed and the proposal event are consecutive
    events = await rows(
        world.sm, "SELECT seq, type FROM room_events WHERE seq > :s ORDER BY seq", s=seq_before
    )
    assert [e["type"] for e in events] == [
        "document_created",
        "active_document_changed",
        "ai_proposal_updated",
    ]
    assert [e["seq"] for e in events] == list(range(seq_before + 1, seq_before + 4))
    # a retry creates nothing twice; a second request id finds the proposal already published
    retry = await world.owner.post(url, json={"client_request_id": key})
    assert (
        retry.status_code == 200
        and retry.json()["document_id"] == doc_id
        and retry.json()["created"] is False
    )
    other = await world.owner.post(url, json={"client_request_id": str(uuid.uuid4())})
    assert (other.status_code, other.json()["error"]["code"]) == (409, "proposal_applied")
    assert await scalar(world.sm, "SELECT count(*) FROM documents") == before_docs + 1


async def test_a_draft_can_be_rejected_by_its_requester_or_the_owner_and_never_published_after(
    world,
):
    prop = await make_proposal(world, kind="draft")
    r = await world.owner.post(f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/reject", json={})
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    gone = await world.owner.post(
        f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/publish",
        json={"client_request_id": str(uuid.uuid4())},
    )
    assert (gone.status_code, gone.json()["error"]["code"]) == (409, "proposal_rejected")


async def test_an_expired_draft_cannot_be_published(world):
    prop = await make_proposal(world, kind="draft")
    await sql(
        world.sm,
        "UPDATE ai_proposals SET expires_at = now() - interval '1 second', created_at = now() - interval '1 day'",
    )
    got = await world.owner.post(
        f"/rooms/{world.room_id}/ai-proposals/{prop['id']}/publish",
        json={"client_request_id": str(uuid.uuid4())},
    )
    assert (got.status_code, got.json()["error"]["code"]) == (409, "proposal_expired")
    assert await scalar(world.sm, "SELECT count(*) FROM documents") == 1


async def test_pending_proposals_expire_with_an_event_and_resolved_ones_are_deleted_after_retention(
    world,
):
    from app.services import ai_proposals as svc

    prop = await make_proposal(world)
    other = await make_proposal(world)
    await sql(
        world.sm,
        "UPDATE ai_proposals SET expires_at = now() - interval '1 second', created_at = now() - interval '1 day' WHERE id = :i",
        i=prop["id"],
    )
    await sql(
        world.sm,
        "UPDATE ai_proposals SET state='applying', applying_by=created_by, applying_since=now() WHERE id = :i",
        i=other["id"],
    )
    assert await svc.expire_due(world.sm) == 1
    states = {
        str(r["id"]): r["state"] for r in await rows(world.sm, "SELECT id, state FROM ai_proposals")
    }
    assert states == {str(prop["id"]): "expired", str(other["id"]): "applying"}
    assert (await events_of_type(world, "ai_proposal_updated"))[-1]["payload"] == {
        "proposal_id": str(prop["id"]),
        "state": "expired",
    }
    assert await svc.expire_due(world.sm) == 0
    # retention: only RESOLVED proposals older than the window go; pending and applying never
    await sql(
        world.sm,
        "UPDATE ai_proposals SET resolved_at = now() - interval '40 days' WHERE id = :i",
        i=prop["id"],
    )
    assert await svc.delete_old(world.sm, 30) == 1
    assert await scalar(world.sm, "SELECT count(*) FROM ai_proposals") == 1


async def test_cleanup_ai_also_removes_old_resolved_proposals(world):
    from app.commands.cleanup_ai import cleanup_ai

    prop = await make_proposal(world)
    await sql(
        world.sm,
        "UPDATE ai_proposals SET state='rejected', resolved_at = now() - interval '45 days' WHERE id = :i",
        i=prop["id"],
    )
    result = await cleanup_ai(world.sm, world.store, proposal_retention_days=30)
    assert result.proposals_deleted == 1


async def test_proposal_events_are_in_the_history_and_carry_ids_and_states_only(world):
    prop = await make_proposal(world)
    r = await world.owner.get(f"/rooms/{world.room_id}/events", params={"after_seq": 0})
    assert r.status_code == 200, r.text
    items = [e for e in r.json()["items"] if e["event_type"] == "ai_proposal_updated"]
    assert items and items[0]["resource_type"] == "ai_proposal"
    assert items[0]["payload"] == {"proposal_id": str(prop["id"]), "state": "pending"}
    assert items[0]["resource_id"] == str(prop["id"])
