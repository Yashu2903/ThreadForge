"""More `@AI` request behaviour: source events, retention and cleanup, fairness of the request queue,
the estimate with files, and the ledger link between a request and its provider calls."""

import uuid

import pytest

from app.commands.cleanup_ai import cleanup_ai
from app.services import ai_requests
from tests import analysis_files as af
from tests.ai_helpers import install
from tests.analysis_helpers import add_member, build_world, give_consent
from tests.helpers import execute as sql
from tests.helpers import rows, scalar

OPS = b"# Ops\n\nThe warehouse processed twelve thousand orders in March.\n"


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def ready(world, name="ops.md", data=OPS):
    source = await world.upload(world.owner, name, data)
    await world.analyze(world.owner, source)
    assert set(await world.run_worker()) == {"succeeded"}
    return source


async def source_events(world):
    return [
        (r["payload"]["state"], r["resource_type"])
        for r in await rows(
            world.sm,
            "SELECT payload, resource_type FROM room_events WHERE type = 'source_updated' ORDER BY seq",
        )
    ]


# ---- source events -----------------------------------------------------------------------------------------


async def test_a_source_announces_creation_readiness_and_removal(world):
    source = await ready(world)
    sid, _vid = world.version_of(source)
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")
    assert await source_events(world) == [
        ("created", "source"),
        ("ready", "source"),
        ("removed", "source"),
    ]


async def test_a_failed_analysis_is_announced_with_its_state_only(world):
    world.stub.transcript = "[illegible]"
    bad = await world.upload(world.owner, "junk.pdf", af.noise_scan_pdf(2))
    await world.analyze(world.owner, bad)
    assert await world.run_worker() == ["failed:too_many_unreadable_pages"]
    states = [s for s, _ in await source_events(world)]
    assert states == ["created", "failed"]
    payloads = await rows(
        world.sm, "SELECT payload::text AS p FROM room_events WHERE type = 'source_updated'"
    )
    assert all(
        "junk" not in p["p"] and "unreadable" not in p["p"] for p in payloads
    )  # no name, no reason


async def test_a_replacement_announces_the_new_version(world):
    source = await ready(world)
    sid, _ = world.version_of(source)
    data = b"# Ops\n\nNew text.\n"
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
    assert [s for s, _ in await source_events(world)] == ["created", "ready", "created"]


# ---- the estimate with files --------------------------------------------------------------------------------------


async def test_the_estimate_counts_the_analysis_of_files_not_analyzed_yet(world):
    fresh = await world.upload(world.owner, "doc.pdf", af.long_text_pdf(4, 3))
    done = await ready(world)
    url = f"/rooms/{world.room_id}/ai-requests/estimate"
    base = {
        "instruction": "How many orders?",
        "task_hint": "ask",
        "document_id": world.document["id"],
    }
    without = (await world.owner.post(url, json=base)).json()["estimate"]
    ready_only = (
        await world.owner.post(
            url, json={**base, "source_version_ids": [world.version_of(done)[1]]}
        )
    ).json()["estimate"]
    unanalyzed = (
        await world.owner.post(
            url, json={**base, "source_version_ids": [world.version_of(fresh)[1]]}
        )
    ).json()["estimate"]
    assert ready_only["new_analysis_pages"] == 0 and unanalyzed["new_analysis_pages"] == 3
    assert unanalyzed["provider_calls"] > ready_only["provider_calls"] >= without["provider_calls"]
    assert unanalyzed["expected_cost_micros"] > ready_only["expected_cost_micros"]
    bogus = await world.owner.post(url, json={**base, "source_version_ids": [str(uuid.uuid4())]})
    assert bogus.status_code == 409 and bogus.json()["error"]["code"] == "source_unavailable"


# ---- the ledger and the request -------------------------------------------------------------------------------------


async def test_every_provider_call_of_a_request_is_ledgered_under_its_id(world):
    source = await ready(world)
    world.stub.classify_as = "summarize"
    rid = (await world.send(world.owner, "Summarize the file", sources=[source])).json()["id"]
    await world.drain()
    ops = [
        r["operation"]
        for r in await rows(
            world.sm,
            "SELECT operation FROM ai_usage_ledger WHERE request_id = :r ORDER BY id",
            r=uuid.UUID(rid),
        )
    ]
    assert ops[0] == "classify" and "map" in ops and ops[-1] == "answer"
    assert (
        await scalar(
            world.sm,
            "SELECT count(*) FROM ai_usage_ledger WHERE request_id = :r AND status <> 'ok'",
            r=uuid.UUID(rid),
        )
        == 0
    )
    usage = (await rows(world.sm, "SELECT usage FROM ai_results"))[0]["usage"]
    assert usage["llm_calls"] == len([o for o in ops if o in ("classify", "map", "answer")])


# ---- the request queue --------------------------------------------------------------------------------------------------


async def test_queued_requests_are_taken_fairly_across_rooms(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    from tests.doc_helpers import room_with_doc

    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_max_active_requests_per_user=10
    )

    a = await ready(world)
    first = (
        await world.send(world.owner, "q1 warehouse orders", sources=[a], task_hint="ask")
    ).json()["id"]
    second = (
        await world.send(world.owner, "q2 warehouse orders", sources=[a], task_hint="ask")
    ).json()["id"]
    third = (
        await world.send(world.owner, "q3 warehouse orders", sources=[a], task_hint="ask")
    ).json()["id"]
    other_room, other_doc = await room_with_doc(world.owner, sessionmaker)
    saved, world.room_id = world.room_id, other_room
    doc_saved, world.document = world.document, other_doc
    b = await ready(world, "b.md", b"# B\n\nwarehouse orders in the other room\n")
    other = (
        await world.send(world.owner, "other warehouse orders", sources=[b], task_hint="ask")
    ).json()["id"]
    world.room_id, world.document = saved, doc_saved
    got = []
    for _ in range(2):
        claimed = await ai_requests.claim_next(world.sm, world.cfg)
        got.append(str(claimed[0].id))
    # the first claim is the oldest overall; the second jumps to the other room, ahead of q2 and q3
    assert got == [first, other]
    assert second and third


async def test_a_request_whose_worker_died_is_taken_over_by_another_after_the_lease(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    first = await ai_requests.claim_next(world.sm, world.cfg)
    assert first is not None
    assert await ai_requests.claim_next(world.sm, world.cfg) is None  # still leased
    await sql(world.sm, "UPDATE ai_requests SET lease_expires_at = now() - interval '1 second'")
    second = await ai_requests.claim_next(world.sm, world.cfg)
    assert second is not None and second[1] != first[1] and str(second[0].id) == rid
    assert await ai_requests.renew(world.sm, world.cfg, second[0].id, first[1]) == "lost_claim"
    assert await ai_requests.renew(world.sm, world.cfg, second[0].id, second[1]) is None


# ---- retention and cleanup ------------------------------------------------------------------------------------------------


async def test_cleanup_deletes_manifests_past_retention_and_keeps_the_answer(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    await world.drain()
    assert await scalar(world.sm, "SELECT count(*) FROM ai_manifests") == 1
    keep = await cleanup_ai(world.sm, world.store)
    assert keep.manifests_deleted == 0  # retention has not passed
    await sql(world.sm, "UPDATE ai_requests SET retain_until = now() - interval '1 day'")
    result = await cleanup_ai(world.sm, world.store)
    assert result.manifests_deleted == 1
    assert await scalar(world.sm, "SELECT count(*) FROM ai_manifests") == 0
    published = (await world.request(world.owner, rid, "/result")).json()
    assert published["answer"]["blocks"] and published["citations"][0]["excerpt"]
    assert len(published["citations"][0]["excerpt"]) <= 300
    assert (await world.request(world.owner, rid, "/context")).json()[
        "context"
    ] is None  # nothing left to summarize
    assert (await cleanup_ai(world.sm, world.store)).manifests_deleted == 0  # idempotent


async def test_a_superseded_index_pinned_by_an_unfinished_request_is_never_deleted(world):
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    pinned = await scalar(world.sm, "SELECT generation_id FROM ai_request_sources")
    await sql(
        world.sm,
        "UPDATE index_generations SET state = 'superseded', published_at = now() - interval '3 days'",
    )
    result = await cleanup_ai(world.sm, world.store)
    assert result.generations_removed == 0
    assert (
        await scalar(world.sm, "SELECT count(*) FROM index_generations WHERE id = :g", g=pinned)
        == 1
    )
    # once the request has finished, the old index goes
    await world.owner.post(f"/rooms/{world.room_id}/ai-requests/{rid}/cancel")
    assert (await cleanup_ai(world.sm, world.store)).generations_removed == 1
    # and the pin does not block it: the finished request merely loses its (unused) generation link
    assert await scalar(world.sm, "SELECT generation_id FROM ai_request_sources") is None


async def test_cleanup_drops_old_budget_windows_and_rate_buckets_only(world):
    await sql(
        world.sm,
        "INSERT INTO ai_budget_windows (scope, scope_key, window_start, spent_micros) VALUES "
        "('global', '', current_date - 100, 5), ('global', 'x', current_date, 7)",
    )
    await sql(
        world.sm,
        "INSERT INTO ai_rate_buckets (bucket, window_start, count) VALUES ('old', now() - interval '3 days', 1), ('new', now(), 1)",
    )
    result = await cleanup_ai(world.sm, world.store)
    assert result.budget_rows_deleted == 2
    assert [
        r["scope_key"] for r in await rows(world.sm, "SELECT scope_key FROM ai_budget_windows")
    ] == ["x"]
    assert [r["bucket"] for r in await rows(world.sm, "SELECT bucket FROM ai_rate_buckets")] == [
        "new"
    ]


async def test_the_viewer_never_gets_an_ai_request_row_to_read(world, make_actor):
    viewer = await make_actor("Viewer", app_settings=world.cfg)
    install(viewer, world.stub)
    await add_member(world.sm, world.room_id, viewer.id, "viewer")
    await give_consent(viewer)
    source = await ready(world)
    rid = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    await world.drain()
    for suffix in ("", "/context"):
        r = await world.request(viewer, rid, suffix)
        assert r.status_code == 403
    page = (await viewer.get(f"/rooms/{world.room_id}/messages")).json()["items"]
    assert {m["kind"] for m in page} == {"ai_request", "ai_answer"}  # they see the chat messages
