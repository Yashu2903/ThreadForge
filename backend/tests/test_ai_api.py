"""AI checkpoint 1, part B: the HTTP surface.

Status, consent, personal keys (encryption, validation, rate limits, no leakage), the room switch and
its event, the usage page, the estimate, and the permission table for the AI gate. Real FastAPI app,
real PostgreSQL, HTTP-level provider stubs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
import pytest_asyncio

from app.ai import keys as ai_keys
from app.ai.notice import NOTICE_VERSION
from app.errors import AppError
from app.services.permissions import decide_write
from tests.ai_helpers import (
    SERVER_GOOGLE_KEY,
    SERVER_OPENAI_KEY,
    USER_GOOGLE_KEY,
    USER_OPENAI_KEY,
    Stub,
    ai_settings,
    install,
    key_in,
)
from tests.doc_helpers import create_doc, room_with_doc
from tests.helpers import add_member, execute, rows
from tests.test_invitations_api import new_room


@pytest.fixture
def ai_cfg(redis_prefix):
    return ai_settings(redis_channel_prefix=redis_prefix, collab_max_state_bytes=65536)


@pytest.fixture
def stub():
    return Stub()


@pytest_asyncio.fixture
async def ai_actor(make_actor, ai_cfg, stub):
    async def factory(name: str):
        actor = await make_actor(name, app_settings=ai_cfg)
        install(actor, stub)
        return actor

    return factory


@pytest_asyncio.fixture
async def owner(ai_actor):
    return await ai_actor("Owner")


@pytest_asyncio.fixture
async def editor(ai_actor):
    return await ai_actor("Editor")


@pytest_asyncio.fixture
async def viewer(ai_actor):
    return await ai_actor("Viewer")


async def consent(actor):
    r = await actor.post("/ai/consent", json={"notice_version": NOTICE_VERSION})
    assert r.status_code == 204, r.text


@pytest_asyncio.fixture
async def ready(sessionmaker, owner, editor, viewer):
    """A room with a document, an editor and a viewer, and consent given by owner and editor."""
    room_id, doc = await room_with_doc(owner, sessionmaker, (editor, "editor"), (viewer, "viewer"))
    await consent(owner)
    await consent(editor)
    return room_id, doc["id"]


def estimate_body(doc_id, **over):
    body = {"instruction": "Summarize the plan", "document_id": doc_id, "task_hint": "ask"}
    body.update(over)
    return body


async def estimate(actor, room_id, doc_id, **over):
    return await actor.post(
        f"/rooms/{room_id}/ai-requests/estimate", json=estimate_body(doc_id, **over)
    )


# ---- status ------------------------------------------------------------------------------------


async def test_status_requires_login_and_reveals_no_secrets(client, owner):
    assert (await client.get("/ai/status")).status_code == 401
    response = await owner.get("/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True and body["globally_enabled"] is True
    assert body["available"] is True and body["code"] == "ok"
    # nothing has been self-checked yet: "not_checked" is reported honestly, never "ok"
    assert {c["state"] for c in body["capabilities"].values()} == {"not_checked"}
    assert body["spend_controls"] == {"prices_configured": True, "budgets_enforced": False}
    assert not key_in(response.text) and "encryption" not in response.text.lower()


async def test_unconfigured_server_says_so(make_actor):
    actor = await make_actor("Plain")  # the default test settings: AI_ENABLED is false
    body = (await actor.get("/ai/status")).json()
    assert body["available"] is False and body["code"] == "ai_not_configured"


# ---- consent -----------------------------------------------------------------------------------


async def test_consent_flow(owner):
    notice = (await owner.get("/ai/consent")).json()
    assert notice["notice_version"] == NOTICE_VERSION and notice["accepted"] is False
    text = " ".join(notice["points"])
    assert "embedding provider (Google)" in text and "generation provider (OpenAI)" in text
    assert "other members" in text.lower() and "own API key or the server" in text
    assert notice["key_sources"]["generation"]["source"] == "server"  # no personal key yet
    assert (await owner.post("/ai/consent", json={"notice_version": 999})).status_code == 409
    assert (
        await owner.post("/ai/consent", json={"notice_version": NOTICE_VERSION}, csrf=False)
    ).status_code == 403
    await consent(owner)
    await consent(owner)  # idempotent
    assert (await owner.get("/ai/consent")).json()["accepted"] is True
    saved = await rows(
        owner_sm(owner), "SELECT user_id, notice_version, accepted_at FROM ai_consents"
    )
    assert len(saved) == 1 and saved[0]["notice_version"] == NOTICE_VERSION


def owner_sm(actor):
    return actor.client._transport.app.state.sessionmaker


# ---- personal keys -----------------------------------------------------------------------------


async def test_saved_key_is_validated_encrypted_and_never_returned(
    owner, stub, sessionmaker, ai_cfg, caplog
):
    caplog.set_level(logging.DEBUG)
    response = await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    assert response.status_code == 200, response.text
    meta = response.json()
    assert meta["provider"] == "openai" and meta["last4"] == USER_OPENAI_KEY[-4:]
    assert meta["state"] == "active" and not key_in(response.text)
    listed = await owner.get("/ai/keys")
    assert listed.json()["items"][0]["last4"] == USER_OPENAI_KEY[-4:] and not key_in(listed.text)

    # validated with exactly ONE cheap provider call, using the key itself
    [call] = stub.calls("/v1/models/")
    assert call["headers"]["authorization"] == f"Bearer {USER_OPENAI_KEY}"
    assert stub.calls("/v1/responses") == []  # no tokens were generated to validate

    [row] = await rows(sessionmaker, "SELECT * FROM user_provider_keys")
    assert USER_OPENAI_KEY.encode() not in bytes(row["encrypted_key"])
    assert row["key_id"] == "t1" and row["last4"] == USER_OPENAI_KEY[-4:]
    # the ledger records the validation, charged to the user's own key
    [entry] = await rows(sessionmaker, "SELECT operation, key_source, status FROM ai_usage_ledger")
    assert (entry["operation"], entry["key_source"], entry["status"]) == (
        "key_validate",
        "user",
        "ok",
    )
    # the plaintext is not in any log line, any table cell, or any API output
    assert USER_OPENAI_KEY not in caplog.text
    for table in ("user_provider_keys", "ai_usage_ledger", "ai_consents"):
        dump = str(await rows(sessionmaker, f"SELECT * FROM {table}"))  # noqa: S608
        assert USER_OPENAI_KEY not in dump

    # a worker can decrypt it, for this user and this provider only
    plain = await ai_keys.load_api_key(sessionmaker, ai_cfg, owner.id, "openai", "user")
    assert plain == USER_OPENAI_KEY
    with pytest.raises(AppError) as other_provider:
        await ai_keys.load_api_key(sessionmaker, ai_cfg, owner.id, "google", "user")
    assert other_provider.value.code == "key_unavailable"


async def test_a_ciphertext_copied_to_another_user_cannot_be_decrypted(
    owner, editor, sessionmaker, ai_cfg
):
    await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    await execute(
        sessionmaker,
        "INSERT INTO user_provider_keys (user_id, provider, encrypted_key, key_id, last4, state) "
        "SELECT :e, provider, encrypted_key, key_id, last4, state FROM user_provider_keys WHERE user_id = :o",
        e=editor.id,
        o=owner.id,
    )
    with pytest.raises(AppError) as err:
        await ai_keys.load_api_key(sessionmaker, ai_cfg, editor.id, "openai", "user")
    assert err.value.code == "key_unavailable"  # AAD binds user + provider


async def test_a_rejected_key_is_not_stored(owner, stub, sessionmaker):
    stub.valid_keys.discard(USER_OPENAI_KEY)
    response = await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    assert response.status_code == 422 and response.json()["error"]["code"] == "key_rejected"
    assert not key_in(response.text)
    assert await rows(sessionmaker, "SELECT 1 FROM user_provider_keys") == []
    [entry] = await rows(sessionmaker, "SELECT status, error_code FROM ai_usage_ledger")
    assert (entry["status"], entry["error_code"]) == ("failed", "key_rejected")

    stub.valid_keys.discard(USER_GOOGLE_KEY)  # Google answers 400 API_KEY_INVALID
    response = await owner.put("/ai/keys/google", json={"api_key": USER_GOOGLE_KEY})
    assert response.status_code == 422 and response.json()["error"]["code"] == "key_rejected"


async def test_provider_outage_is_not_blamed_on_the_key(owner, stub, sessionmaker):
    stub.llm_failures = [(503, {}, {})]
    response = await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    assert (
        response.status_code == 502 and response.json()["error"]["code"] == "provider_unavailable"
    )
    assert await rows(sessionmaker, "SELECT 1 FROM user_provider_keys") == []


@pytest.mark.parametrize(
    "bad", ["", "short", "has space in it 123456789012", "ünïcode-key-0123456789012345", "x" * 500]
)
async def test_malformed_keys_are_refused_before_any_provider_call(owner, stub, bad):
    response = await owner.put("/ai/keys/openai", json={"api_key": bad})
    assert response.status_code == 422
    assert stub.requests == []


async def test_unknown_provider_csrf_and_auth(owner, client, stub):
    assert (
        await owner.put("/ai/keys/anthropic", json={"api_key": USER_OPENAI_KEY})
    ).status_code == 422
    assert (
        await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY}, csrf=False)
    ).status_code == 403
    assert (await client.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})).status_code in (
        401,
        403,
    )
    assert (await owner.delete("/ai/keys/anthropic")).status_code == 422
    assert stub.requests == []


async def test_keys_are_private_and_deletable(owner, editor, sessionmaker):
    await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    assert (await editor.get("/ai/keys")).json()["items"] == []  # not visible to others
    assert (await editor.delete("/ai/keys/openai")).status_code == 204  # deleting "own" key: no-op
    assert len((await owner.get("/ai/keys")).json()["items"]) == 1  # untouched
    assert (await owner.delete("/ai/keys/openai")).status_code == 204
    assert (await owner.get("/ai/keys")).json()["items"] == []
    assert await rows(sessionmaker, "SELECT 1 FROM user_provider_keys") == []


async def test_saving_replaces_the_previous_key(owner, stub, sessionmaker, ai_cfg):
    second = "sk-user-second-0123456789abcdefXYZ1"
    stub.valid_keys.add(second)
    await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    r = await owner.put("/ai/keys/openai", json={"api_key": second})
    assert r.status_code == 200 and r.json()["last4"] == "XYZ1"
    assert len(await rows(sessionmaker, "SELECT 1 FROM user_provider_keys")) == 1
    assert await ai_keys.load_api_key(sessionmaker, ai_cfg, owner.id, "openai", "user") == second


async def test_key_validation_attempts_are_rate_limited_per_user_and_per_ip(
    make_actor, sessionmaker, stub, redis_prefix
):
    cfg = ai_settings(
        redis_channel_prefix=redis_prefix,
        ai_key_validations_per_user_per_hour=3,
        ai_key_validations_per_ip_per_hour=100,
    )
    actor = await make_actor("Tester", app_settings=cfg)
    install(actor, stub)
    codes = []
    for i in range(5):
        # wrong keys count too: the limit exists to stop key GUESSING
        r = await actor.put(
            "/ai/keys/openai", json={"api_key": f"sk-guess-{i:02d}-0123456789abcdef"}
        )
        codes.append(r.status_code)
    assert codes == [422, 422, 422, 429, 429]
    assert "retry-after" in r.headers
    assert len(stub.calls("/v1/models/")) == 3  # the throttled attempts never reached the provider

    cfg2 = ai_settings(
        redis_channel_prefix=redis_prefix,
        ai_key_validations_per_user_per_hour=100,
        ai_key_validations_per_ip_per_hour=2,
    )
    a, b = await make_actor("A", app_settings=cfg2), await make_actor("B", app_settings=cfg2)
    install(a, stub)
    install(b, stub)
    results = [
        (
            await who.put("/ai/keys/openai", json={"api_key": f"sk-ip-{i}-0123456789abcdefgh"})
        ).status_code
        for i, who in enumerate((a, b, a, b))
    ]
    assert results.count(429) >= 2  # the same network address shares one budget across users


async def test_key_policies(owner, sessionmaker, make_actor, redis_prefix, stub):
    async def choice(policy, provider="openai", save=False):
        cfg = ai_settings(redis_channel_prefix=redis_prefix, ai_key_policy=policy)
        async with sessionmaker() as db:
            return await ai_keys.choose_key_source(db, cfg, owner.id, provider)

    assert (await choice("user_if_present")).source == "server"
    assert (await choice("user_only")).problem == "key_unavailable"
    await owner.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    assert (await choice("user_if_present")).source == "user"
    assert (await choice("user_only")).source == "user"
    assert (await choice("server_only")).source == "server"  # personal keys are ignored
    assert (await choice("user_if_present", "google")).source == "server"  # per provider
    # a key the provider rejected at run time is NOT silently replaced by the server key
    await ai_keys.mark_rejected(sessionmaker, owner.id, "openai")
    assert (await choice("user_if_present")).problem == "key_rejected"
    with pytest.raises(AppError) as err:
        await ai_keys.load_api_key(sessionmaker, ai_settings(), owner.id, "openai", "user")
    assert err.value.code == "key_rejected"
    # a deleted key: the request that chose "user" fails, it does not fall back to "server"
    await owner.delete("/ai/keys/openai")
    with pytest.raises(AppError) as gone:
        await ai_keys.load_api_key(sessionmaker, ai_settings(), owner.id, "openai", "user")
    assert gone.value.code == "key_unavailable"


async def test_server_only_policy_refuses_personal_keys(make_actor, redis_prefix, stub):
    cfg = ai_settings(redis_channel_prefix=redis_prefix, ai_key_policy="server_only")
    actor = await make_actor("S", app_settings=cfg)
    install(actor, stub)
    r = await actor.put("/ai/keys/openai", json={"api_key": USER_OPENAI_KEY})
    assert r.status_code == 409 and r.json()["error"]["code"] == "user_keys_not_allowed"


# ---- the room switch ---------------------------------------------------------------------------


async def test_room_switch_owner_only_with_event(ready, owner, editor, viewer, carol, sessionmaker):
    room_id, _doc = ready
    url = f"/rooms/{room_id}/ai-settings"
    assert (await viewer.get(url)).json() == {"ai_enabled": True, "available": True, "code": "ok"}
    assert (await editor.put(url, json={"ai_enabled": False})).status_code == 403
    assert (await viewer.put(url, json={"ai_enabled": False})).status_code == 403
    assert (await carol.put(url, json={"ai_enabled": False})).status_code == 404
    assert (await carol.get(url)).status_code == 404
    assert (await owner.put(url, json={"ai_enabled": False}, csrf=False)).status_code == 403
    assert (await owner.put(url, json={"ai_enabled": False, "extra": 1})).status_code == 422

    before = await rows(sessionmaker, "SELECT version, membership_epoch, updated_at FROM rooms")
    r = await owner.put(url, json={"ai_enabled": False})
    assert r.status_code == 200 and r.json()["ai_enabled"] is False
    assert r.json()["code"] == "ai_disabled_for_room"
    after = await rows(sessionmaker, "SELECT version, membership_epoch, updated_at FROM rooms")
    assert before == after  # not a rename, not an access change
    await owner.put(url, json={"ai_enabled": False})  # same value: no second event
    events = await rows(
        sessionmaker,
        "SELECT type, resource_type, resource_id, payload, resource_version FROM room_events "
        "WHERE type = 'room_ai_settings_changed'",
    )
    assert len(events) == 1
    assert (
        events[0]["payload"] == {"ai_enabled": False} and str(events[0]["resource_id"]) == room_id
    )
    assert events[0]["resource_version"] is None
    # the event is also queued for publication like every room event
    assert await rows(
        sessionmaker,
        "SELECT 1 FROM outbox o JOIN room_events e USING (event_id) WHERE e.type = 'room_ai_settings_changed'",
    )
    await owner.put(url, json={"ai_enabled": True})
    assert (
        len(
            await rows(
                sessionmaker, "SELECT 1 FROM room_events WHERE type = 'room_ai_settings_changed'"
            )
        )
        == 2
    )


# ---- the permission table for AI requests ------------------------------------------------------


async def test_owner_and_editor_can_estimate_viewer_and_outsiders_cannot(
    ready, owner, editor, viewer, carol
):
    room_id, doc_id = ready
    for actor in (owner, editor):
        r = await estimate(actor, room_id, doc_id)
        assert r.status_code == 200, r.text
    r = await estimate(viewer, room_id, doc_id)
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_role"
    r = await estimate(carol, room_id, doc_id)
    assert r.status_code == 404 and r.json()["error"]["code"] == "room_not_found"


async def test_removed_member_has_no_access(ready, editor, sessionmaker):
    room_id, doc_id = ready
    await add_member(sessionmaker, room_id, editor.id, "editor", state="removed")
    assert (await estimate(editor, room_id, doc_id)).status_code == 404


async def test_downgraded_editor_loses_ai_at_once(ready, editor, sessionmaker):
    room_id, doc_id = ready
    assert (await estimate(editor, room_id, doc_id)).status_code == 200
    await add_member(sessionmaker, room_id, editor.id, "viewer")
    assert (await estimate(editor, room_id, doc_id)).json()["error"]["code"] == "insufficient_role"


async def test_anchor_document_rules(ready, owner, editor, sessionmaker):
    room_id, doc_id = ready
    body = {"instruction": "hi", "task_hint": "ask"}
    r = await owner.post(f"/rooms/{room_id}/ai-requests/estimate", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_anchor_required"
    r = await estimate(owner, room_id, str(uuid.uuid4()))
    assert r.status_code == 404 and r.json()["error"]["code"] == "document_not_found"
    # a document that belongs to ANOTHER room is "not found" here (and the owner of both rooms is
    # not an exception: the anchor must be in the room the request is made in)
    other_room = await new_room(owner, "Other")
    other_doc = (await create_doc(owner, other_room)).json()["id"]
    r = await estimate(owner, room_id, other_doc)
    assert r.status_code == 404 and r.json()["error"]["code"] == "document_not_found"


async def test_ai_write_access_required_when_the_document_is_not_writable(
    ready, owner, sessionmaker
):
    room_id, doc_id = ready
    await execute(
        sessionmaker, "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d", d=doc_id
    )
    r = await estimate(owner, room_id, doc_id)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ai_write_access_required"
    await execute(
        sessionmaker, "UPDATE documents SET collab_state = 'ready' WHERE id = :d", d=doc_id
    )
    assert (await estimate(owner, room_id, doc_id)).status_code == 200  # healed: writable again


async def test_size_locked_document_is_not_writable(ready, owner, sessionmaker, ai_cfg):
    room_id, doc_id = ready
    await execute(
        sessionmaker,
        "INSERT INTO document_yjs_state (document_id, room_id, state, update_counter, byte_size, updated_at)"
        " VALUES (:d, :r, decode(repeat('00', :n), 'hex'), 1, :n, now())",
        d=doc_id,
        r=room_id,
        n=ai_cfg.collab_max_state_bytes,
    )
    r = await estimate(owner, room_id, doc_id)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ai_write_access_required"


async def test_decide_write_reasons_and_the_collab_authorize_share_it(
    ready, owner, editor, viewer, carol, sessionmaker, ai_cfg
):
    room_id, doc_id = ready
    doc = uuid.UUID(doc_id)
    async with sessionmaker() as db:
        assert (await decide_write(db, ai_cfg, owner.id, doc)).allowed
        assert (await decide_write(db, ai_cfg, editor.id, doc)).allowed
        v = await decide_write(db, ai_cfg, viewer.id, doc)
        assert (v.allowed, v.reason, v.role_ok) == (False, "insufficient_role", False)
        n = await decide_write(db, ai_cfg, carol.id, doc)
        assert (n.allowed, n.reason) == (False, "not_member")
        assert (
            await decide_write(db, ai_cfg, owner.id, uuid.uuid4())
        ).reason == "document_not_found"
        assert (await decide_write(db, ai_cfg, uuid.uuid4(), doc)).reason == "user_inactive"
    await execute(sessionmaker, "UPDATE users SET state = 'disabled' WHERE id = :u", u=editor.id)
    async with sessionmaker() as db:
        assert (await decide_write(db, ai_cfg, editor.id, doc)).reason == "user_inactive"
    await execute(
        sessionmaker, "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d", d=doc_id
    )
    async with sessionmaker() as db:
        d = await decide_write(db, ai_cfg, owner.id, doc)
        # AI is refused, but a person may still edit (that is how a needs_repair document heals)
        assert (d.allowed, d.reason, d.role_ok) == (False, "needs_repair", True)


async def test_collab_authorize_uses_the_same_function(ready, owner, viewer, sessionmaker, ai_cfg):
    from app.services.collab import authorize_document

    room_id, doc_id = ready
    cookie = f"{ai_cfg.session_cookie_name}={owner.secret}"
    access = await authorize_document(sessionmaker, ai_cfg, cookie, uuid.UUID(doc_id))
    assert access.can_edit and access.role == "owner" and access.room_id == uuid.UUID(room_id)
    access = await authorize_document(
        sessionmaker, ai_cfg, f"{ai_cfg.session_cookie_name}={viewer.secret}", uuid.UUID(doc_id)
    )
    assert not access.can_edit and access.role == "viewer"
    await execute(
        sessionmaker, "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d", d=doc_id
    )
    again = await authorize_document(sessionmaker, ai_cfg, cookie, uuid.UUID(doc_id))
    assert again.can_edit and again.collab_state == "needs_repair"  # people can still heal it


# ---- switches, availability, consent in the gate ------------------------------------------------


async def test_gate_failures_have_distinct_codes(
    ready, owner, sessionmaker, make_actor, redis_prefix, stub
):
    room_id, doc_id = ready
    # 1. the room switch
    await owner.put(f"/rooms/{room_id}/ai-settings", json={"ai_enabled": False})
    r = await estimate(owner, room_id, doc_id)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "ai_disabled_for_room")
    await owner.put(f"/rooms/{room_id}/ai-settings", json={"ai_enabled": True})
    # 2. the global kill switch
    await execute(
        sessionmaker,
        "INSERT INTO ai_global_flags (id, ai_enabled) VALUES (1, false)"
        " ON CONFLICT (id) DO UPDATE SET ai_enabled = false",
    )
    r = await estimate(owner, room_id, doc_id)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "ai_disabled")
    assert (await owner.get("/ai/status")).json()["code"] == "ai_disabled"
    await execute(sessionmaker, "DELETE FROM ai_global_flags")  # no row = on
    # 3. a failed REQUIRED capability
    await execute(
        sessionmaker,
        "INSERT INTO ai_capability_status (capability, ok, code) VALUES ('text_embedding', false, 'dimension_mismatch')",
    )
    r = await estimate(owner, room_id, doc_id)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "ai_unavailable")
    assert r.json()["error"]["capability_code"] == "dimension_mismatch"
    # ... but a failed OPTIONAL capability leaves AI usable
    await execute(sessionmaker, "DELETE FROM ai_capability_status")
    await execute(
        sessionmaker,
        "INSERT INTO ai_capability_status (capability, ok, code) VALUES ('image_embedding', false, 'pair_ranking_failed')",
    )
    assert (await estimate(owner, room_id, doc_id)).status_code == 200
    # 4. not configured at all
    plain = await make_actor("Plain")
    r = await plain.post(
        f"/rooms/{await new_room(plain)}/ai-requests/estimate", json={"instruction": "x"}
    )
    assert r.json()["error"]["code"] == "ai_not_configured"


# ---- the estimate ------------------------------------------------------------------------------


async def test_estimate_reports_size_cost_and_key_source(ready, owner, sessionmaker):
    room_id, doc_id = ready
    r = await estimate(owner, room_id, doc_id)
    body = r.json()
    est = body["estimate"]
    assert est["provider_calls"] == 1 and est["cost_known"] is True
    assert est["input_tokens"] >= 800 and est["max_output_tokens"] == 2000
    assert est["max_cost_micros"] > est["expected_cost_micros"] > 0
    assert body["key_sources"]["generation"] == {
        "provider": "openai",
        "source": "server",
        "problem": None,
    }
    assert (
        body["consent_given"] is True and body["can_send"] is True and body["blocking_code"] is None
    )
    # the estimate calls no provider and writes nothing
    assert await rows(sessionmaker, "SELECT 1 FROM ai_usage_ledger") == []

    auto = (await estimate(owner, room_id, doc_id, task_hint="auto")).json()["estimate"]
    assert auto["provider_calls"] == 2  # + the small classification call

    long_text = "x" * 30_000
    big = (await estimate(owner, room_id, doc_id, instruction=long_text[:4000])).json()["estimate"]
    assert big["input_tokens"] > est["input_tokens"]
    whole = (
        await estimate(
            owner, room_id, doc_id, include_document_content=True, document_scope="whole"
        )
    ).json()["estimate"]
    assert whole["input_tokens"] > est["input_tokens"]
    blocks = (
        await estimate(
            owner,
            room_id,
            doc_id,
            include_document_content=True,
            document_scope="blocks",
            selected_chars=30_000,
        )
    ).json()["estimate"]
    assert blocks["input_tokens"] > 10_000


async def test_estimate_flags_caps_and_requires_confirmation_when_large(ready, owner, sessionmaker):
    room_id, doc_id = ready
    r = await estimate(
        owner,
        room_id,
        doc_id,
        include_document_content=True,
        document_scope="blocks",
        selected_chars=1_000_000,
    )
    body = r.json()
    assert "max_input_tokens" in body["estimate"]["over_caps"]
    assert body["can_send"] is False and body["blocking_code"] == "caps_exceeded"
    mid = (
        await estimate(
            owner,
            room_id,
            doc_id,
            include_document_content=True,
            document_scope="blocks",
            selected_chars=100_000,
        )
    ).json()
    assert mid["estimate"]["over_caps"] == [] and mid["estimate"]["requires_confirmation"] is False
    small_threshold = ai_settings(ai_confirm_threshold_tokens=1000)
    from app.ai.estimate import Estimate, needs_confirmation

    base = Estimate(900, 500, 2000, 1, 0, False, None, None, False)
    assert needs_confirmation(small_threshold, base)  # 1400 tokens > 1000 (no prices known: tokens)
    priced = ai_settings(ai_confirm_threshold_micros=100)
    assert needs_confirmation(priced, Estimate(1, 1, 1, 1, 0, True, 101, 200, False))
    assert not needs_confirmation(priced, Estimate(1, 1, 1, 1, 0, True, 100, 200, False))


async def test_estimate_validates_its_input(ready, owner):
    room_id, doc_id = ready
    assert (await estimate(owner, room_id, doc_id, instruction="")).status_code == 422
    assert (await estimate(owner, room_id, doc_id, instruction="x" * 4001)).status_code == 422
    assert (await estimate(owner, room_id, doc_id, task_hint="write")).status_code == 422
    assert (await estimate(owner, room_id, doc_id, actor_id=str(uuid.uuid4()))).status_code == 422
    assert (await estimate(owner, room_id, doc_id, document_scope="whole")).status_code == 422
    ids = [str(uuid.uuid4()) for _ in range(21)]
    assert (
        await estimate(owner, room_id, doc_id, include_chat_context=True, chat_message_ids=ids)
    ).status_code == 422
    assert (await estimate(owner, room_id, doc_id, chat_message_ids=ids[:2])).status_code == 422
    r = await owner.post(
        f"/rooms/{room_id}/ai-requests/estimate", json=estimate_body(doc_id), csrf=False
    )
    assert r.status_code == 403


# ---- the usage page ----------------------------------------------------------------------------


async def test_usage_page_is_owner_only_and_aggregates_the_ledger(
    ready, owner, editor, viewer, sessionmaker
):
    room_id, _doc = ready
    for who, cost, source, status in (
        (owner, 100, "server", "ok"),
        (editor, 50, "user", "ok"),
        (editor, 0, "server", "failed"),
    ):
        await execute(
            sessionmaker,
            "INSERT INTO ai_usage_ledger (user_id, charged_user_id, room_id, provider, model, operation,"
            " input_tokens, output_tokens, est_cost_micros, key_source, status)"
            " VALUES (:u, :u, :r, 'openai', 'm', 'answer', 10, 5, :c, :k, :s)",
            u=who.id,
            r=room_id,
            c=cost,
            k=source,
            s=status,
        )
    for forbidden in (editor, viewer):
        assert (await forbidden.get(f"/rooms/{room_id}/ai-usage")).status_code == 403
    body = (await owner.get(f"/rooms/{room_id}/ai-usage")).json()
    assert body["totals"]["calls"] == 3 and body["totals"]["cost_micros"] == 150
    assert body["totals"]["failed_calls"] == 1
    by_user = {r["label"]: r for r in body["by_user"]}
    assert by_user["Owner"]["cost_micros"] == 100 and by_user["Editor"]["calls"] == 2
    assert {r["label"] for r in body["by_key_source"]} == {"server", "user"}
    assert [r["label"] for r in body["by_operation"]] == ["answer"]
    assert len(body["by_day"]) == 1
    assert (await owner.get(f"/rooms/{room_id}/ai-usage?days=0")).status_code == 422


async def test_usage_page_shows_budgets_and_warnings(sessionmaker, make_actor, redis_prefix, stub):
    cfg = ai_settings(
        redis_channel_prefix=redis_prefix,
        ai_budget_room_daily_micros=1000,
        ai_budget_global_monthly_micros=10_000,
    )
    owner = await make_actor("O", app_settings=cfg)
    install(owner, stub)
    room_id = await new_room(owner)
    from datetime import UTC, datetime

    day = datetime.now(UTC).date()
    for scope, key, ws, spent in (
        ("room", room_id, day, 600),
        ("global", "", day.replace(day=1), 100),
    ):
        await execute(
            sessionmaker,
            "INSERT INTO ai_budget_windows (scope, scope_key, window_start, spent_micros)"
            " VALUES (:s, :k, :w, :sp)",
            s=scope,
            k=key,
            w=ws,
            sp=spent,
        )
    await execute(
        sessionmaker,
        "INSERT INTO ai_budget_warnings (scope, scope_key, window_start, threshold) VALUES ('room', :k, :w, 50)",
        k=room_id,
        w=day,
    )
    body = (await owner.get(f"/rooms/{room_id}/ai-usage")).json()
    budgets = {b["scope"]: b for b in body["budgets"]}
    assert budgets["room"]["percent"] == 60.0 and budgets["global"]["percent"] == 1.0
    assert [w["threshold_percent"] for w in body["warnings"]] == [50]


# ---- the self-check ----------------------------------------------------------------------------


async def run_check(sessionmaker, cfg, stub):
    from app.ai.selfcheck import run_selfcheck

    return await run_selfcheck(sessionmaker, cfg, stub.transports())


async def test_selfcheck_all_capabilities_pass_and_are_stored(sessionmaker, ai_cfg, stub, owner):
    import shutil

    results = await run_check(sessionmaker, ai_cfg, stub)
    for name in (
        "text_generation",
        "structured_output",
        "image_input",
        "text_embedding",
        "image_embedding",
    ):
        assert results[name].ok, (name, results[name])
    assert results["ocr_local"].ok == (
        shutil.which("tesseract") is not None and results["ocr_local"].ok
    )
    status = (await owner.get("/ai/status")).json()
    assert status["capabilities"]["image_input"]["state"] == "ok"
    assert status["capabilities"]["text_embedding"]["code"] == "ok"
    # the check used the SERVER keys, and every call is in the ledger without a user
    sent = {
        r["headers"].get("authorization") or r["headers"].get("x-goog-api-key")
        for r in stub.requests
    }
    assert sent == {f"Bearer {SERVER_OPENAI_KEY}", SERVER_GOOGLE_KEY}
    ledger = await rows(
        sessionmaker, "SELECT DISTINCT operation, key_source, user_id FROM ai_usage_ledger"
    )
    assert ledger == [{"operation": "self_check", "key_source": "server", "user_id": None}]
    # the image prompt really contained an image, and the pair test used 2 images + 2 texts
    img_call = [r for r in stub.calls("/v1/responses") if "input_image" in str(r["json"]["input"])]
    assert len(img_call) == 1


async def test_a_failing_capability_disables_only_what_it_should(sessionmaker, ai_cfg, stub, owner):
    stub.break_pair_ranking = True
    results = await run_check(sessionmaker, ai_cfg, stub)
    assert (
        not results["image_embedding"].ok
        and results["image_embedding"].code == "pair_ranking_failed"
    )
    assert results["text_embedding"].ok
    body = (await owner.get("/ai/status")).json()
    assert body["available"] is True  # figures lose image embedding; AI as a whole stays up

    stub.break_pair_ranking = False
    stub.wrong_dimension = True
    results = await run_check(sessionmaker, ai_cfg, stub)
    assert not results["text_embedding"].ok
    body = (await owner.get("/ai/status")).json()
    assert body["available"] is False and body["code"] == "ai_unavailable"


async def test_generation_failure_disables_ai_with_a_safe_code(sessionmaker, ai_cfg, stub, owner):
    stub.llm_failures = [(400, {}, {"error": {"message": f"bad {SERVER_OPENAI_KEY}"}})] * 6
    results = await run_check(sessionmaker, ai_cfg, stub)
    assert (
        not results["text_generation"].ok
        and results["text_generation"].code == "provider_bad_request"
    )
    stored = str(await rows(sessionmaker, "SELECT * FROM ai_capability_status"))
    assert SERVER_OPENAI_KEY not in stored  # provider error text is scrubbed before it is stored
    assert (await owner.get("/ai/status")).json()["code"] == "ai_unavailable"


async def test_wrong_structured_output_fails_the_structured_check(sessionmaker, ai_cfg, stub):
    stub.llm_answer = lambda body: {"color": "purple", "count": "three"}
    results = await run_check(sessionmaker, ai_cfg, stub)
    assert (
        not results["structured_output"].ok
        and results["structured_output"].code == "schema_invalid"
    )


async def test_missing_server_keys_are_not_checked_not_ok(
    sessionmaker, stub, redis_prefix, make_actor
):
    cfg = ai_settings(redis_channel_prefix=redis_prefix, ai_openai_api_key="", ai_google_api_key="")
    results = await run_check(sessionmaker, cfg, stub)
    assert "text_generation" not in results and stub.requests == []
    actor = await make_actor("U", app_settings=cfg)
    caps = (await actor.get("/ai/status")).json()["capabilities"]
    assert caps["text_generation"]["state"] == "not_checked"
    assert caps["ocr_local"]["state"] in ("ok", "failed")


# ---- operator commands -------------------------------------------------------------------------


async def test_operator_kill_switch_commands(ready, owner, sessionmaker, monkeypatch, capsys):
    from app.commands import ai_disable, ai_enable, ai_spend_report, ai_status
    from tests.factories import TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    room_id, doc_id = ready
    assert (await estimate(owner, room_id, doc_id)).status_code == 200
    assert await asyncio.to_thread(ai_disable.main, ["--note", "incident 42"]) == 0
    r = await estimate(owner, room_id, doc_id)  # no restart, no redeploy
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_disabled"
    assert await asyncio.to_thread(ai_status.main, []) == 0
    out = capsys.readouterr().out
    assert "OFF" in out and "incident 42" in out
    assert await asyncio.to_thread(ai_enable.main, []) == 0
    assert (await estimate(owner, room_id, doc_id)).status_code == 200
    await execute(
        sessionmaker,
        "INSERT INTO ai_usage_ledger (user_id, charged_user_id, room_id, provider, model, operation,"
        " est_cost_micros, key_source, status) VALUES (:u, :u, :r, 'openai', 'm', 'answer', 2500000, 'server', 'ok')",
        u=owner.id,
        r=room_id,
    )
    capsys.readouterr()
    assert await asyncio.to_thread(ai_spend_report.main, ["--by", "user"]) == 0
    out = capsys.readouterr().out
    assert "Owner" in out and "2.5000" in out
    assert await asyncio.to_thread(ai_status.main, []) == 0
    assert "spend server" in capsys.readouterr().out
    assert not key_in(out)


async def test_global_flag_survives_a_missing_row(sessionmaker):
    from app.ai.status import load_status, set_global_flag

    await execute(sessionmaker, "DELETE FROM ai_global_flags")
    async with sessionmaker() as db:
        assert (await load_status(db, ai_settings())).globally_enabled is True  # no row = on
    async with sessionmaker() as db, db.begin():
        await set_global_flag(db, False, "x")
    async with sessionmaker() as db:
        assert (await load_status(db, ai_settings())).globally_enabled is False
