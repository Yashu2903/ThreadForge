"""`@AI` request rules: the permission table, validation, idempotency, capacity, confirmation, message
locking, cancel, retry, expiry and what happens when files change under a request."""

import asyncio
import uuid

import pytest

from app.ai.answer import execute, strategies
from app.services import ai_requests
from tests import analysis_files as af
from tests import source_files as sf
from tests.ai_helpers import USER_GOOGLE_KEY, USER_OPENAI_KEY, install
from tests.analysis_helpers import add_member, build_world, give_consent
from tests.helpers import execute as sql
from tests.helpers import rows, scalar

TEXT = b"# Ops\n\nThe warehouse processed twelve thousand orders in March.\n"


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def member(world, make_actor, name, role, *, consent=True, state="active"):
    actor = await make_actor(name, app_settings=world.cfg)
    install(actor, world.stub)
    await add_member(world.sm, world.room_id, actor.id, role, state)
    if consent:
        await give_consent(actor)
    return actor


async def ready(world, name="ops.md", data=TEXT):
    source = await world.upload(world.owner, name, data)
    await world.analyze(world.owner, source)
    assert set(await world.run_worker()) == {"succeeded"}
    return source


def code(response) -> str:
    return response.json()["error"]["code"]


# ---- the permission table ----------------------------------------------------------------------------------


async def test_who_may_send_view_and_read(world, make_actor, client):
    source = await ready(world)
    editor = await member(world, make_actor, "Editor", "editor")
    viewer = await member(world, make_actor, "Viewer", "viewer")
    outsider = await make_actor("Outsider", app_settings=world.cfg)
    removed = await member(world, make_actor, "Removed", "editor", state="removed")
    url = f"/rooms/{world.room_id}/ai-requests"
    body = world.request_body(source_version_ids=[world.version_of(source)[1]])

    assert (await viewer.post(url, json=body)).status_code == 403
    assert code(await viewer.post(url, json=body)) == "insufficient_role"
    assert (await outsider.post(url, json=body)).status_code == 404
    assert (await removed.post(url, json=body)).status_code == 404
    assert (await client.post(url, json=body)).status_code == 401
    assert (await world.owner.request("POST", url, json=body, csrf=False)).status_code == 403

    mine = await world.send(editor, sources=[source], task_hint="ask")
    assert mine.status_code == 201
    rid = mine.json()["id"]
    await world.drain()

    assert (await world.request(editor, rid)).status_code == 200
    assert (await world.request(world.owner, rid)).status_code == 200  # an owner with can_write
    assert (
        await world.request(viewer, rid)
    ).status_code == 403  # viewers see only the chat message
    assert (await world.request(viewer, rid, "/context")).status_code == 403
    assert (await world.request(outsider, rid)).status_code == 404
    assert (await world.request(removed, rid)).status_code == 404
    # ... but a viewer reads the published answer, and an outsider cannot
    assert (await world.request(viewer, rid, "/result")).status_code == 200
    assert (await world.request(outsider, rid, "/result")).status_code == 404
    assert (await world.request(removed, rid, "/result")).status_code == 404


async def test_another_editor_without_write_access_cannot_see_progress(world, make_actor):
    source = await ready(world)
    editor = await member(world, make_actor, "Editor", "editor")
    other = await member(world, make_actor, "Other", "editor")
    rid = (await world.send(editor, sources=[source], task_hint="ask")).json()["id"]
    await sql(
        world.sm,
        "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d",
        d=world.document["id"],
    )
    r = await world.request(other, rid)
    assert r.status_code == 403 and code(r) == "ai_write_access_required"
    assert (
        await world.request(editor, rid)
    ).status_code == 200  # the requester always sees their own


async def test_the_gate_runs_at_admission_with_its_own_codes(world, make_actor):
    source = await ready(world)
    nocon = await member(world, make_actor, "NoConsent", "editor", consent=False)
    r = await world.send(nocon, sources=[source])
    assert r.status_code == 403 and code(r) == "consent_required"

    await sql(
        world.sm,
        "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d",
        d=world.document["id"],
    )
    r = await world.send(world.owner, sources=[source])
    assert r.status_code == 403 and code(r) == "ai_write_access_required"
    await sql(
        world.sm,
        "UPDATE documents SET collab_state = 'ready' WHERE id = :d",
        d=world.document["id"],
    )

    r = await world.send(world.owner, sources=[source], document_id=str(uuid.uuid4()))
    assert r.status_code == 404 and code(r) == "document_not_found"

    await world.owner.put(f"/rooms/{world.room_id}/ai-settings", json={"ai_enabled": False})
    r = await world.send(world.owner, sources=[source])
    assert r.status_code == 409 and code(r) == "ai_disabled_for_room"
    await world.owner.put(f"/rooms/{world.room_id}/ai-settings", json={"ai_enabled": True})

    await sql(
        world.sm,
        "INSERT INTO ai_global_flags (id, ai_enabled) VALUES (1, false) ON CONFLICT (id) DO UPDATE SET ai_enabled = false",
    )
    r = await world.send(world.owner, sources=[source])
    assert r.status_code == 503 and code(r) == "ai_disabled"
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_request'") == 0


async def test_a_document_of_another_room_is_not_an_anchor(world, sessionmaker):
    from tests.doc_helpers import room_with_doc

    source = await ready(world)
    _other_room, other_doc = await room_with_doc(world.owner, sessionmaker)
    r = await world.send(world.owner, sources=[source], document_id=other_doc["id"])
    assert r.status_code == 404 and code(r) == "document_not_found"


# ---- keys ----------------------------------------------------------------------------------------------------


async def test_user_only_without_a_key_is_refused(make_actor, sessionmaker, tmp_path, redis_prefix):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_key_policy="user_only"
    )
    r = await world.send(
        world.owner,
        sources=[],
        include_chat_context=False,
        include_document_content=True,
        document_scope="whole",
    )
    assert r.status_code == 409 and code(r) == "key_unavailable"


async def test_a_personal_key_is_recorded_and_used_and_never_silently_replaced(world):
    for provider, key in (("openai", USER_OPENAI_KEY), ("google", USER_GOOGLE_KEY)):
        await world.owner.put(f"/ai/keys/{provider}", json={"api_key": key})
    source = await ready(world)
    r = await world.send(world.owner, sources=[source], task_hint="ask")
    assert r.json()["key_sources"] == {"generation": "user", "embedding": "user"}
    await world.drain()
    keys_used = {c["headers"].get("authorization") for c in world.stub.calls("/v1/responses")}
    assert keys_used == {f"Bearer {USER_OPENAI_KEY}"}
    ledger = await rows(
        world.sm, "SELECT DISTINCT key_source FROM ai_usage_ledger WHERE request_id IS NOT NULL"
    )
    assert ledger == [{"key_source": "user"}]

    # a second request; the personal key is deleted before the worker runs: it FAILS, no fallback
    await world.send(world.owner, "Another question", sources=[source], task_hint="ask")
    for provider in ("openai", "google"):
        await world.owner.delete(f"/ai/keys/{provider}")
    calls = world.stub.provider_call_count()
    assert await world.drain() == ["request:failed:key_unavailable"]
    assert world.stub.provider_call_count() == calls


# ---- validation -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "status", "needle"),
    [
        ({}, 422, "ai_context_required"),
        ({"include_chat_context": True}, 422, "chat_context_mismatch"),
        ({"chat_message_ids": [str(uuid.uuid4())]}, 422, "chat_context_mismatch"),
        ({"include_document_content": True}, 422, "document_scope"),
        ({"document_scope": "whole"}, 422, "document_scope"),
        ({"include_document_content": True, "document_scope": "blocks"}, 422, "selected_blocks"),
        (
            {
                "include_document_content": True,
                "document_scope": "blocks",
                "selected_block_ids": [str(uuid.uuid4())],
            },
            422,
            "selection_hash",
        ),
        (
            {
                "include_document_content": True,
                "document_scope": "whole",
                "selected_block_ids": [str(uuid.uuid4())],
            },
            422,
            "selected_blocks",
        ),
        ({"instruction": ""}, 422, "validation_error"),
        ({"instruction": "x" * 4001}, 422, "validation_error"),
        ({"instruction": "\x00\x01  "}, 422, "validation_error"),
        ({"task_hint": "write"}, 422, "validation_error"),
        ({"requested_by": str(uuid.uuid4())}, 422, "validation_error"),
        ({"key_source": "server"}, 422, "validation_error"),
        ({"selection_hash": "ABC"}, 422, "validation_error"),
    ],
)
async def test_invalid_requests_are_refused_and_create_nothing(world, overrides, status, needle):
    source = await ready(world)
    body = world.request_body()
    if (
        "include_document_content" not in overrides
        and "document_scope" not in overrides
        and "chat_message_ids" not in overrides
        and "include_chat_context" not in overrides
        and not overrides.keys()
        & {
            "instruction",
            "task_hint",
            "requested_by",
            "key_source",
            "selection_hash",
            "selected_block_ids",
        }
    ):
        pass
    body.update(overrides)
    r = await world.owner.post(f"/rooms/{world.room_id}/ai-requests", json=body)
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert needle in (err["code"], err.get("reason")), err
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_request'") == 0
    assert source


async def test_source_lists_are_validated(world, make_actor, sessionmaker):
    from tests.doc_helpers import room_with_doc

    source = await ready(world)
    vid = world.version_of(source)[1]
    r = await world.send(world.owner, source_version_ids=[vid, vid])
    assert r.status_code == 422 and r.json()["error"]["reason"] == "duplicate_sources"
    r = await world.send(world.owner, source_version_ids=[str(uuid.uuid4()) for _ in range(11)])
    assert r.status_code == 422
    r = await world.send(world.owner, source_version_ids=[str(uuid.uuid4())])
    assert r.status_code == 409 and code(r) == "source_unavailable"
    # a version of another room
    other_room, other_doc = await room_with_doc(world.owner, sessionmaker)
    saved, world.room_id = world.room_id, other_room
    foreign = await world.upload(world.owner, "foreign.md", b"# X\n\nforeign text\n")
    world.room_id = saved
    r = await world.send(world.owner, source_version_ids=[world.version_of(foreign)[1]])
    assert r.status_code == 409 and code(r) == "source_unavailable"
    # a removed source
    sid, _ = world.version_of(source)
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")
    r = await world.send(world.owner, source_version_ids=[vid])
    assert r.status_code == 409 and code(r) == "source_unavailable"


async def test_chat_context_rules(world, make_actor):
    other = await member(world, make_actor, "Chatter", "editor")
    msg = await world.owner.post(
        f"/rooms/{world.room_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "hello team"},
    )
    mid = msg.json()["id"]
    source = await ready(world)
    only_chat = await world.send(world.owner, include_chat_context=True, chat_message_ids=[mid])
    assert only_chat.status_code == 422 and code(only_chat) == "ai_context_required"
    r = await world.send(
        world.owner, sources=[source], include_chat_context=True, chat_message_ids=[mid]
    )
    assert r.status_code == 201 and r.json()["chat_message_count"] == 1
    # unknown, other-room-style and deleted messages are refused
    r = await world.send(
        world.owner,
        sources=[source],
        include_chat_context=True,
        chat_message_ids=[str(uuid.uuid4())],
    )
    assert r.status_code == 422 and r.json()["error"]["reason"] == "chat_messages"
    gone = await world.owner.post(
        f"/rooms/{world.room_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "oops"},
    )
    assert (
        await world.owner.delete(
            f"/rooms/{world.room_id}/messages/{gone.json()['id']}", params={"expected_version": 1}
        )
    ).status_code == 204
    r = await world.send(
        world.owner,
        sources=[source],
        include_chat_context=True,
        chat_message_ids=[gone.json()["id"]],
    )
    assert r.status_code == 422
    # an AI answer is never chat context
    await world.drain()
    answer_rows = await rows(world.sm, "SELECT id FROM messages WHERE kind = 'ai_answer' LIMIT 1")
    answer_msg = answer_rows[0]["id"] if answer_rows else None
    if answer_msg:
        r = await world.send(
            world.owner,
            sources=[source],
            include_chat_context=True,
            chat_message_ids=[str(answer_msg)],
        )
        assert r.status_code == 422
    assert other


# ---- idempotency ----------------------------------------------------------------------------------------------------


async def test_the_same_request_id_returns_the_same_request(world):
    source = await ready(world)
    body = world.request_body(source_version_ids=[world.version_of(source)[1]])
    url = f"/rooms/{world.room_id}/ai-requests"
    first = await world.owner.post(url, json=body)
    again = await world.owner.post(url, json=body)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 1
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_request'") == 1
    changed = await world.owner.post(url, json={**body, "instruction": "Something else entirely"})
    assert changed.status_code == 409 and code(changed) == "ai_request_id_reused"
    other_source = await world.owner.post(url, json={**body, "source_version_ids": []})
    assert other_source.status_code in (409, 422)


async def test_parallel_duplicates_create_exactly_one_request_and_one_message(world):
    source = await ready(world)
    body = world.request_body(source_version_ids=[world.version_of(source)[1]])
    url = f"/rooms/{world.room_id}/ai-requests"
    results = await asyncio.gather(*(world.owner.post(url, json=body) for _ in range(6)))
    assert sorted(r.status_code for r in results) == [200, 200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in results}) == 1
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 1
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_request'") == 1


async def test_a_retry_after_a_demotion_cannot_bypass_the_role_check(world, make_actor):
    source = await ready(world)
    editor = await member(world, make_actor, "Editor", "editor")
    body = world.request_body(source_version_ids=[world.version_of(source)[1]])
    url = f"/rooms/{world.room_id}/ai-requests"
    assert (await editor.post(url, json=body)).status_code == 201
    await add_member(world.sm, world.room_id, editor.id, "viewer")
    assert (await editor.post(url, json=body)).status_code == 403


# ---- capacity, rate limit, confirmation ---------------------------------------------------------------------------


async def test_active_requests_are_capped_per_user_and_per_room(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor,
        sessionmaker,
        tmp_path,
        redis_prefix,
        ai_max_active_requests_per_user=2,
        ai_max_active_requests_per_room=3,
    )
    other = await member(world, make_actor, "Other", "editor")
    pending = [
        await world.upload(world.owner, f"p{i}.md", f"# P{i}\n\ntext {i}\n".encode())
        for i in range(5)
    ]
    sent = [await world.send(world.owner, f"q{i}", sources=[pending[i]]) for i in range(3)]
    assert [r.status_code for r in sent] == [201, 201, 429]
    assert sent[2].json()["error"] == {
        **sent[2].json()["error"],
        "code": "ai_capacity",
        "scope": "user",
        "limit": 2,
    }
    assert (await world.send(other, "other q", sources=[pending[3]])).status_code == 201
    r = await world.send(other, "other q2", sources=[pending[4]])
    assert r.status_code == 429 and r.json()["error"]["scope"] == "room"
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 3
    # finishing one frees capacity
    await world.drain()
    assert (await world.send(world.owner, "again", sources=[pending[0]])).status_code == 201


async def test_an_ai_message_counts_as_a_chat_write_and_a_refusal_consumes_nothing(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, message_writes_per_minute=1
    )
    src = await world.upload(world.owner, "a.md", b"# A\n\ntext\n")
    assert (await world.send(world.owner, "first", sources=[src])).status_code == 201
    r = await world.send(world.owner, "second", sources=[src])
    assert r.status_code == 429 and code(r) == "rate_limited"
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 1
    assert (
        await scalar(world.sm, "SELECT count(*) FROM analysis_job_waiters") == 1
    )  # the refused one queued nothing


async def test_a_large_request_needs_confirmation_and_creates_nothing_until_confirmed(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_confirm_threshold_micros=1
    )
    src = await ready(world)
    r = await world.send(world.owner, sources=[src])
    assert r.status_code == 409 and code(r) == "confirmation_required"
    assert r.json()["error"]["estimate"]["requires_confirmation"] is True
    assert await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 0
    body = world.request_body(source_version_ids=[world.version_of(src)[1]])
    url = f"/rooms/{world.room_id}/ai-requests"
    assert (await world.owner.post(url, json=body)).status_code == 409
    assert (await world.owner.post(url, json={**body, "confirm_large": True})).status_code == 201


async def test_analysis_caps_apply_to_everything_a_request_would_trigger(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_max_new_analysis_pages=4
    )
    a = await world.upload(world.owner, "a.pdf", sf.text_pdf(3))
    b = await world.upload(world.owner, "b.pdf", sf.text_pdf(3))
    r = await world.send(world.owner, sources=[a, b])
    assert r.status_code == 409 and code(r) == "analysis_cap_exceeded"
    assert r.json()["error"]["actual"] == 6
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 0
    assert (await world.send(world.owner, sources=[a])).status_code == 201


# ---- AI messages are locked ------------------------------------------------------------------------------------------


async def test_ai_messages_cannot_be_edited_and_an_edit_never_reruns_ai(world):
    source = await ready(world)
    r = await world.send(world.owner, sources=[source], task_hint="ask")
    await world.drain()
    msg_id = r.json()["message_id"]
    edit = await world.owner.patch(
        f"/rooms/{world.room_id}/messages/{msg_id}",
        json={"body": "@AI changed", "expected_version": 1},
    )
    assert edit.status_code == 409 and code(edit) == "message_locked"
    reply = await scalar(world.sm, "SELECT id FROM messages WHERE kind = 'ai_answer'")
    edit = await world.owner.patch(
        f"/rooms/{world.room_id}/messages/{reply}", json={"body": "x", "expected_version": 1}
    )
    assert edit.status_code in (403, 409)  # not the author (the system user); locked either way
    assert await world.drain() == []  # nothing reran
    plain = await world.owner.post(
        f"/rooms/{world.room_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "hi @AI do things"},
    )
    assert plain.status_code == 201
    edit = await world.owner.patch(
        f"/rooms/{world.room_id}/messages/{plain.json()['id']}",
        json={"body": "hi again @AI", "expected_version": 1},
    )
    assert edit.status_code == 200
    assert (
        await scalar(world.sm, "SELECT count(*) FROM ai_requests") == 1
    )  # text containing @AI never triggers anything


async def test_messages_carry_their_kind_everywhere_clients_read_them(world):
    source = await ready(world)
    r = await world.send(world.owner, sources=[source], task_hint="ask")
    await world.drain()
    page = (await world.owner.get(f"/rooms/{world.room_id}/messages")).json()["items"]
    kinds = [(m["kind"], m["ai_request_id"] is not None) for m in page]
    assert ("ai_request", True) in kinds and ("ai_answer", True) in kinds
    sync = (await world.owner.get(f"/rooms/{world.room_id}/sync")).json()
    assert {m["kind"] for m in sync["messages"]} >= {"ai_request", "ai_answer"}
    assert r


async def test_deleting_the_ai_message_cancels_an_unfinished_request_and_its_analysis(world):
    src = await world.upload(world.owner, "a.md", b"# A\n\ntext about orders\n")
    r = await world.send(world.owner, sources=[src])
    assert r.json()["state"] == "waiting_for_sources"
    d = await world.owner.delete(
        f"/rooms/{world.room_id}/messages/{r.json()['message_id']}", params={"expected_version": 1}
    )
    assert d.status_code == 204
    assert (await rows(world.sm, "SELECT state FROM ai_requests"))[0]["state"] == "cancelled"
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs"))[0]["state"] == "cancelled"
    assert await world.drain() == []


async def test_deleting_an_ai_answer_needs_the_owner_and_redacts_the_stored_result(
    world, make_actor
):
    editor = await member(world, make_actor, "Editor", "editor")
    source = await ready(world)
    r = await world.send(editor, sources=[source], task_hint="ask")
    await world.drain()
    reply = await scalar(world.sm, "SELECT id FROM messages WHERE kind = 'ai_answer'")
    denied = await editor.delete(
        f"/rooms/{world.room_id}/messages/{reply}", params={"expected_version": 1}
    )
    assert denied.status_code == 403
    ok = await world.owner.delete(
        f"/rooms/{world.room_id}/messages/{reply}", params={"expected_version": 1}
    )
    assert ok.status_code == 204
    result = (await world.request(world.owner, r.json()["id"], "/result")).json()
    assert result["redacted"] is True and result["answer"] is None
    assert all("excerpt" not in c and c["redacted"] for c in result["citations"])
    assert (await rows(world.sm, "SELECT redacted_at IS NOT NULL AS r FROM ai_manifests"))[0][
        "r"
    ] is True


# ---- cancel, retry, expiry -------------------------------------------------------------------------------------------


async def test_cancelling_a_queued_request(world, make_actor):
    source = await ready(world)
    editor = await member(world, make_actor, "Editor", "editor")
    viewer = await member(world, make_actor, "Viewer", "viewer")
    r = await world.send(editor, sources=[source], task_hint="ask")
    rid = r.json()["id"]
    url = f"/rooms/{world.room_id}/ai-requests/{rid}/cancel"
    other = await member(world, make_actor, "Other", "editor")
    assert (await other.post(url)).status_code == 403
    assert (await viewer.post(url)).status_code == 403
    assert (await editor.post(url)).json()["state"] == "cancelled"
    assert (await world.owner.post(url)).status_code == 409  # already finished
    assert await world.drain() == []
    assert (await rows(world.sm, "SELECT count(*) AS n FROM messages WHERE kind = 'ai_answer'"))[0][
        "n"
    ] == 0


async def test_the_owner_can_cancel_anyones_request(world, make_actor):
    editor = await member(world, make_actor, "Editor", "editor")
    src = await world.upload(editor, "a.md", b"# A\n\nsome orders text\n")
    rid = (await world.send(editor, sources=[src])).json()["id"]
    assert (await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/cancel")).json()[
        "state"
    ] == "cancelled"
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs"))[0][
        "state"
    ] == "cancelled"  # nobody waits any more


async def test_cancelling_a_running_request_stops_it_before_it_publishes(world, monkeypatch):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    real = strategies.gather_retrieval

    async def gather_then_cancel(*args, **kwargs):
        result = await real(*args, **kwargs)
        assert (await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/cancel")).json()[
            "state"
        ] == "cancelling"
        return result

    monkeypatch.setattr(strategies, "gather_retrieval", gather_then_cancel)
    assert await world.drain() == ["request:cancelled"]
    assert (await rows(world.sm, "SELECT state FROM ai_requests"))[0]["state"] == "cancelled"
    assert await scalar(world.sm, "SELECT count(*) FROM ai_results") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 0
    assert world.stub.vision_calls("cited_answer") == 0  # no answer call was made after the cancel


async def test_a_result_that_arrives_after_a_cancel_is_discarded_but_its_usage_stays(
    world, monkeypatch
):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    real = execute._answer

    async def answer_then_cancel(*args, **kwargs):
        validated = await real(*args, **kwargs)
        await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/cancel")
        return validated

    monkeypatch.setattr(execute, "_answer", answer_then_cancel)
    assert await world.drain() == ["request:cancelled"]
    assert await scalar(world.sm, "SELECT count(*) FROM ai_results") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 0
    assert (
        await scalar(
            world.sm,
            "SELECT count(*) FROM ai_usage_ledger WHERE operation = 'answer' AND status = 'ok'",
        )
        == 1
    )


async def test_a_failed_request_can_be_retried_by_its_requester_up_to_the_limit(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_request_max_attempts=3
    )
    other = await member(world, make_actor, "Other", "editor")
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    url = f"/rooms/{world.room_id}/ai-requests/{rid}/retry"
    assert (await world.owner.post(url)).status_code == 409  # nothing failed yet

    for attempt in (1, 2):
        world.stub.llm_failures = [(400, {}, {"error": {"message": "bad"}})]
        assert await world.drain() == ["request:failed:provider_bad_request"]
        view = (await world.request(world.owner, rid)).json()
        assert (
            view["state"] == "failed" and view["can_retry"] is True and view["attempt"] == attempt
        )
        assert (await other.post(url)).status_code == 403  # only the requester
        retried = await world.owner.post(url)
        assert (
            retried.status_code == 200
            and retried.json()["attempt"] == attempt + 1
            and retried.json()["state"] == "queued"
        )
    world.stub.llm_failures = [(400, {}, {"error": {"message": "bad"}})]
    await world.drain()
    last = await world.owner.post(url)
    assert last.status_code == 409 and last.json()["error"]["reason"] == "attempts_exhausted"
    assert (await rows(world.sm, "SELECT count(*) AS n FROM ai_manifests"))[0]["n"] >= 1


async def test_a_retry_succeeds_and_publishes_once(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    world.stub.llm_failures = [(400, {}, {"error": {"message": "bad"}})]
    await world.drain()
    assert (
        await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/retry")
    ).status_code == 200
    assert await world.drain() == ["request:published"]
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 1
    assert await scalar(world.sm, "SELECT count(*) FROM ai_results") == 1


async def test_a_retry_rechecks_everything_and_pins_nothing_new(world):
    source = await ready(world)
    sid, v1 = world.version_of(source)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    world.stub.llm_failures = [(400, {}, {"error": {"message": "bad"}})]
    await world.drain()
    # a newer version of the file appears: the retry keeps version 1
    data = b"# Ops\n\nThe harbour processed nothing at all.\n"
    r = await world.owner.post(
        f"/rooms/{world.room_id}/source-uploads",
        json={
            "client_request_id": str(uuid.uuid4()),
            "filename": "ops.md",
            "byte_size": len(data),
            "source_id": sid,
        },
    )
    uid = r.json()["id"]
    await world.owner.put(
        f"/rooms/{world.room_id}/source-uploads/{uid}/content",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    await world.owner.post(f"/rooms/{world.room_id}/source-uploads/{uid}/finalize")
    await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/retry")
    pins = await rows(world.sm, "SELECT source_version_id FROM ai_request_sources")
    assert [str(p["source_version_id"]) for p in pins] == [v1]
    await world.drain()
    manifest = (
        await rows(world.sm, "SELECT data FROM ai_manifests ORDER BY attempt DESC LIMIT 1")
    )[0]["data"]
    assert "warehouse" in str(manifest["passages"]) and "harbour" not in str(manifest["passages"])


async def test_a_retry_after_consent_was_withdrawn_is_refused(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    world.stub.llm_failures = [(400, {}, {"error": {"message": "bad"}})]
    await world.drain()
    await sql(world.sm, "DELETE FROM ai_consents WHERE user_id = :u", u=world.owner.id)
    assert (
        code(await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/retry"))
        == "consent_required"
    )


async def test_a_retry_after_the_file_was_removed_is_refused(world):
    source = await ready(world)
    sid, _ = world.version_of(source)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    world.stub.llm_failures = [(400, {}, {"error": {"message": "bad"}})]
    await world.drain()
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")
    r = await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/retry")
    assert r.status_code == 409 and code(r) == "source_unavailable"


async def test_a_request_waiting_too_long_expires_and_releases_its_analysis(world):
    src = await world.upload(world.owner, "a.md", b"# A\n\ntext about orders\n")
    rid = (await world.send(world.owner, sources=[src])).json()["id"]
    await sql(world.sm, "UPDATE ai_requests SET created_at = now() - interval '31 minutes'")
    assert await ai_requests.expire_stale(world.sm, world.cfg) == 1
    view = (await world.request(world.owner, rid)).json()
    assert (
        view["state"] == "expired"
        and view["error_code"] == "request_expired"
        and view["can_retry"] is True
    )
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs"))[0]["state"] == "cancelled"
    assert (await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/retry")).json()[
        "state"
    ] == "waiting_for_sources"


# ---- files change under a request -----------------------------------------------------------------------------------


async def test_a_source_removed_while_the_request_waits_fails_it_clearly(world):
    src = await world.upload(world.owner, "a.md", b"# A\n\ntext about orders\n")
    sid, vid = world.version_of(src)
    rid = (await world.send(world.owner, sources=[src])).json()["id"]
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")
    await ai_requests.advance_waiting(world.sm, world.cfg)
    view = (await world.request(world.owner, rid)).json()
    assert view["state"] == "failed" and view["error_code"] == "source_removed"
    assert view["error_detail"] == {"source_version_ids": [vid]}


async def test_a_file_that_fails_analysis_stops_the_request_it_is_never_skipped(world):
    world.stub.transcript = "[illegible]"
    bad = await world.upload(world.owner, "junk.pdf", af.noise_scan_pdf(2))
    good = await ready(world)
    rid = (await world.send(world.owner, "Compare these", sources=[good, bad])).json()["id"]
    outcomes = await world.drain()
    assert "analysis:failed:too_many_unreadable_pages" in outcomes
    view = (await world.request(world.owner, rid)).json()
    assert view["state"] == "failed" and view["error_code"] == "source_failed"
    assert view["error_detail"]["sources"][0]["error_code"] == "too_many_unreadable_pages"
    assert (
        world.stub.vision_calls("cited_answer") == 0
    )  # nothing was answered from the good file alone


async def test_a_source_removed_while_the_request_runs_fails_it_and_publishes_nothing(
    world, monkeypatch
):
    source = await ready(world)
    sid, _ = world.version_of(source)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    real = execute._answer

    async def answer_then_remove(*args, **kwargs):
        validated = await real(*args, **kwargs)
        await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")
        return validated

    monkeypatch.setattr(execute, "_answer", answer_then_remove)
    assert await world.drain() == ["request:failed:source_removed"]
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 0
    assert (await world.request(world.owner, rid)).json()["error_code"] == "source_removed"


async def test_a_request_keeps_the_exact_version_it_pinned(world):
    source = await ready(world)
    sid, v1 = world.version_of(source)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    data = b"# Ops\n\nThe harbour handled zero containers.\n"
    r = await world.owner.post(
        f"/rooms/{world.room_id}/source-uploads",
        json={
            "client_request_id": str(uuid.uuid4()),
            "filename": "ops.md",
            "byte_size": len(data),
            "source_id": sid,
        },
    )
    uid = r.json()["id"]
    await world.owner.put(
        f"/rooms/{world.room_id}/source-uploads/{uid}/content",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    await world.owner.post(f"/rooms/{world.room_id}/source-uploads/{uid}/finalize")
    await world.drain()
    result = (await world.request(world.owner, rid, "/result")).json()
    assert result["citations"][0]["source_version_id"] == v1
    assert "warehouse" in result["citations"][0]["excerpt"]


# ---- exactly one reply ----------------------------------------------------------------------------------------------


async def test_a_replacement_worker_cannot_publish_a_second_answer(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    assert await world.drain() == ["request:published"]
    # a second worker takes over a lease the first one had not finished with, and runs again
    token = uuid.uuid4()
    await sql(
        world.sm,
        "UPDATE ai_requests SET state = 'running', claim_token = :t, finished_at = NULL, "
        "lease_expires_at = now() + interval '1 minute' WHERE id = :i",
        t=token,
        i=rid,
    )
    row = (await rows(world.sm, "SELECT * FROM ai_requests WHERE id = :i", i=rid))[0]

    class R:  # the row as the claim returns it
        def __init__(self, d):
            self.__dict__.update(d)

    outcome = await execute.run_request(
        world.sm, world.cfg, world.store, R(row), token, transports=world.stub.transports()
    )
    assert outcome == "already_published"
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 1
    assert await scalar(world.sm, "SELECT count(*) FROM ai_results") == 1
    assert (await rows(world.sm, "SELECT state FROM ai_requests"))[0]["state"] == "succeeded"


async def test_a_worker_that_lost_its_claim_changes_nothing(world):
    source = await ready(world)
    await world.send(world.owner, sources=[source], task_hint="ask")
    claimed = await ai_requests.claim_next(world.sm, world.cfg)
    assert claimed is not None
    row, token = claimed
    # another worker took the request over: the first one's token is no longer valid
    await sql(world.sm, "UPDATE ai_requests SET claim_token = :t", t=uuid.uuid4())
    assert await ai_requests.renew(world.sm, world.cfg, row.id, token) == "lost_claim"
    pub = ai_requests.Publication("qa", {"blocks": []}, [], {}, {}, {}, "late")
    assert await ai_requests.publish_success(world.sm, world.cfg, row, token, pub) == "lost"
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 0


async def test_a_dead_workers_request_is_taken_over_then_failed_when_claims_run_out(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    for _ in range(ai_requests.MAX_CLAIMS):
        assert await ai_requests.claim_next(world.sm, world.cfg) is not None
        await sql(world.sm, "UPDATE ai_requests SET lease_expires_at = now() - interval '1 second'")
    assert await ai_requests.claim_next(world.sm, world.cfg) is None
    assert await ai_requests.sweep_abandoned(world.sm, world.cfg) == 1
    view = (await world.request(world.owner, rid)).json()
    assert view["state"] == "failed" and view["error_code"] == "execution_attempts_exhausted"


async def test_the_worker_stops_before_any_model_access_when_the_requester_loses_access(
    world, make_actor
):
    source = await ready(world)
    editor = await member(world, make_actor, "Editor", "editor")
    rid = (await world.send(editor, sources=[source], task_hint="ask")).json()["id"]
    await add_member(world.sm, world.room_id, editor.id, "viewer")
    calls = world.stub.provider_call_count()
    assert await world.drain() == ["request:failed:insufficient_role"]
    assert world.stub.provider_call_count() == calls
    assert (await world.request(world.owner, rid)).json()["error_code"] == "insufficient_role"


async def test_access_lost_while_the_model_thinks_is_caught_before_publishing(
    world, make_actor, monkeypatch
):
    source = await ready(world)
    editor = await member(world, make_actor, "Editor", "editor")
    rid = (await world.send(editor, sources=[source], task_hint="ask")).json()["id"]
    real = execute._answer

    async def answer_then_demote(*args, **kwargs):
        validated = await real(*args, **kwargs)
        await add_member(world.sm, world.room_id, editor.id, "viewer")
        return validated

    monkeypatch.setattr(execute, "_answer", answer_then_demote)
    assert await world.drain() == ["request:failed:insufficient_role"]
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 0
    assert rid


async def test_a_budget_that_runs_out_fails_the_request_and_publishes_nothing(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)
    source = await ready(world)
    # now a daily budget so small that the answer call cannot be reserved
    world.cfg = world.cfg.model_copy(update={"ai_budget_user_daily_micros": 1})
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    assert await world.drain() == ["request:failed:budget_exhausted"]
    assert (await world.request(world.owner, rid)).json()["error_code"] == "budget_exhausted"
    assert (
        await scalar(world.sm, "SELECT coalesce(sum(reserved_micros), 0) FROM ai_budget_windows")
        == 0
    )
    assert await scalar(world.sm, "SELECT count(*) FROM messages WHERE kind = 'ai_answer'") == 0
