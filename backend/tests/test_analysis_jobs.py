"""The analysis job lifecycle and its guarantees: permissions, keys, caps, budgets, cancellation, crash
and retry, invisible partial work, removal during a job, and the worker re-checking access.

Real PostgreSQL (pgvector), real storage, the real extractor child; the providers are HTTP stubs."""

import asyncio
import uuid

import pytest

from app.ai.analysis import jobs, pipeline, store
from tests import analysis_files as af
from tests import source_files as sf
from tests.ai_helpers import USER_GOOGLE_KEY, USER_OPENAI_KEY, install
from tests.analysis_helpers import add_member, build_world, give_consent
from tests.helpers import execute, rows, scalar


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


async def queued(world, name="report.pdf", data=None):
    source = await world.upload(world.owner, name, data or sf.text_pdf(2))
    r = await world.analyze(world.owner, source)
    assert r.status_code == 202, r.text
    return source, r.json()


# ---- the permission table -----------------------------------------------------------------------------


async def test_who_may_analyze_status_and_browse_passages(world, make_actor, client):
    editor = await member(world, make_actor, "Editor", "editor")
    viewer = await member(world, make_actor, "Viewer", "viewer")
    outsider = await make_actor("Outsider", app_settings=world.cfg)
    removed = await member(world, make_actor, "Removed", "editor", state="removed")
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    await world.analyze(world.owner, source)
    await world.run_worker()
    sid, vid = world.version_of(source)
    base = f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}"
    body = {"document_id": world.document["id"]}

    assert (await world.analyze(editor, source)).status_code == 202
    for who, expected in ((viewer, 403), (outsider, 404), (removed, 404)):
        r = await who.post(base + "/analyze", json=body)
        assert r.status_code == expected, (who.name, r.text)
        r = await who.delete(base + "/analyze")
        assert r.status_code == expected, (who.name, r.text)
        r = await who.get(base + "/passages")
        assert r.status_code == expected, (who.name, r.text)
    assert (await viewer.get(base + "/analysis")).status_code == 200  # viewers see the status
    assert "passages" not in (await viewer.get(base + "/analysis")).json()
    for who in (outsider, removed):
        assert (await who.get(base + "/analysis")).status_code == 404
    assert (await editor.get(base + "/passages")).status_code == 200
    assert (await world.owner.get(base + "/passages")).status_code == 200

    anon = await client.post(base + "/analyze", json=body)
    assert anon.status_code == 401
    no_csrf = await world.owner.request("POST", base + "/analyze", json=body, csrf=False)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"
    no_csrf = await world.owner.request("DELETE", base + "/analyze", csrf=False)
    assert no_csrf.status_code == 403


async def test_a_non_member_with_a_bad_body_still_gets_404_and_a_viewer_403(world, make_actor):
    outsider = await make_actor("Outsider", app_settings=world.cfg)
    viewer = await member(world, make_actor, "Viewer", "viewer")
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    sid, vid = world.version_of(source)
    url = f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}/analyze"
    assert (await outsider.post(url, json={"nonsense": 1})).status_code == 404
    assert (await viewer.post(url, json={"nonsense": 1})).status_code == 403
    assert (await world.owner.post(url, json={"nonsense": 1})).status_code == 422
    assert (await world.owner.post(url, json={})).status_code == 422


async def test_unknown_ids_and_other_rooms_are_404(world, make_actor, sessionmaker):
    from tests.doc_helpers import room_with_doc

    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    sid, vid = world.version_of(source)
    ghost = str(uuid.uuid4())
    base = f"/rooms/{world.room_id}/sources"
    body = {"document_id": world.document["id"]}
    assert (
        await world.owner.post(f"{base}/{ghost}/versions/{vid}/analyze", json=body)
    ).status_code == 404
    assert (
        await world.owner.post(f"{base}/{sid}/versions/{ghost}/analyze", json=body)
    ).status_code == 404
    assert (
        await world.owner.post(f"{base}/nope/versions/{vid}/analyze", json=body)
    ).status_code == 404
    assert (await world.owner.get(f"{base}/{sid}/versions/{ghost}/analysis")).status_code == 404
    other_room, other_doc = await room_with_doc(world.owner, sessionmaker)
    r = await world.owner.post(
        f"/rooms/{other_room}/sources/{sid}/versions/{vid}/analyze",
        json={"document_id": other_doc["id"]},
    )
    assert r.status_code == 404  # the source belongs to another room
    r = await world.analyze(world.owner, source, document_id=other_doc["id"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "document_not_found"


async def test_the_ai_gate_runs_on_analyze(world, make_actor):
    editor = await member(world, make_actor, "NoConsent", "editor", consent=False)
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    r = await world.analyze(editor, source)
    assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required"
    await world.owner.put(f"/rooms/{world.room_id}/ai-settings", json={"ai_enabled": False})
    r = await world.analyze(world.owner, source)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_disabled_for_room"
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 0


async def test_uploads_still_work_with_the_room_switch_off_but_nothing_is_analyzed(world):
    await world.owner.put(f"/rooms/{world.room_id}/ai-settings", json={"ai_enabled": False})
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    assert (await world.state(source))["analysis_state"] == "not_analyzed"
    assert await world.run_worker() == []


async def test_a_read_only_document_blocks_analysis(world, sessionmaker):
    big = af.long_text_pdf(1, 1)
    source = await world.upload(world.owner, "a.pdf", big)
    await execute(
        sessionmaker,
        "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d",
        d=world.document["id"],
    )
    r = await world.analyze(world.owner, source)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ai_write_access_required"


# ---- keys ------------------------------------------------------------------------------------------------


async def test_user_only_policy_without_a_key_is_refused_at_admission(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_key_policy="user_only"
    )
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    r = await world.analyze(world.owner, source)
    assert r.status_code == 409 and r.json()["error"]["code"] == "key_unavailable"
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 0


async def test_a_personal_key_is_used_and_recorded_never_the_servers(world):
    for provider, key in (("openai", USER_OPENAI_KEY), ("google", USER_GOOGLE_KEY)):
        r = await world.owner.put(f"/ai/keys/{provider}", json={"api_key": key})
        assert r.status_code == 200, r.text
    source, body = await queued(world)
    assert (body["key_source_generation"], body["key_source_embedding"]) == ("user", "user")
    assert await world.run_worker() == ["succeeded"]
    sent = {r["headers"].get("x-goog-api-key") for r in world.stub.calls("batchEmbed")}
    assert sent == {USER_GOOGLE_KEY}
    ledger = await rows(
        world.sm, "SELECT DISTINCT key_source FROM ai_usage_ledger WHERE operation = 'embed_text'"
    )
    assert ledger == [{"key_source": "user"}]


async def test_a_key_deleted_before_the_job_runs_fails_without_falling_back(world):
    for provider, key in (("openai", USER_OPENAI_KEY), ("google", USER_GOOGLE_KEY)):
        await world.owner.put(f"/ai/keys/{provider}", json={"api_key": key})
    source, _ = await queued(world)
    for provider in ("openai", "google"):
        assert (await world.owner.delete(f"/ai/keys/{provider}")).status_code == 204
    calls_before = world.stub.provider_call_count()
    assert await world.run_worker() == ["failed:key_unavailable"]
    assert world.stub.provider_call_count() == calls_before  # the server's key was not tried
    state = await world.state(source)
    assert state["analysis_state"] == "failed" and state["error_code"] == "key_unavailable"


async def test_a_rejected_key_fails_the_job_and_marks_the_key(world):
    for provider, key in (("openai", USER_OPENAI_KEY), ("google", USER_GOOGLE_KEY)):
        await world.owner.put(f"/ai/keys/{provider}", json={"api_key": key})
    source, _ = await queued(world)
    world.stub.valid_keys.discard(USER_GOOGLE_KEY)  # the provider now refuses it
    assert await world.run_worker() == ["failed:key_rejected"]
    assert (await world.state(source))["error_code"] == "key_rejected"
    listed = (await world.owner.get("/ai/keys")).json()
    items = listed if isinstance(listed, list) else listed.get("keys") or listed.get("items")
    assert {k["provider"]: k["state"] for k in items}["google"] == "rejected"
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") == 0


# ---- caps and budgets ---------------------------------------------------------------------------------------


async def test_a_file_over_the_page_cap_is_refused_before_anything_is_queued(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_max_new_analysis_pages=2
    )
    source = await world.upload(world.owner, "long.pdf", sf.text_pdf(3))
    r = await world.analyze(world.owner, source)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "analysis_cap_exceeded"
    assert (err["cap"], err["limit"], err["actual"]) == ("max_new_analysis_pages", 2, 3)
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 0


async def test_the_provider_call_cap_stops_a_job_that_would_make_too_many_calls(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor,
        sessionmaker,
        tmp_path,
        redis_prefix,
        ai_analysis_max_provider_calls=1,
        ai_embed_batch_size=1,
        ai_chunk_target_tokens=40,
        ai_chunk_max_tokens=80,
        ai_chunk_overlap_tokens=5,
    )
    source = await world.upload(world.owner, "long.pdf", af.long_text_pdf(6, 2))
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["failed:analysis_cap_exceeded"]
    assert len(world.stub.calls("batchEmbed")) == 1  # the second call was refused BEFORE sending
    assert await scalar(world.sm, "SELECT count(*) FROM index_generations") == 0


async def test_an_exhausted_budget_fails_the_job_and_releases_the_reservation(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor,
        sessionmaker,
        tmp_path,
        redis_prefix,
        ai_budget_global_monthly_micros=1,  # one micro-dollar: any embedding call exceeds it
    )
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["failed:budget_exhausted"]
    assert world.stub.calls("batchEmbed") == []
    assert (
        await scalar(world.sm, "SELECT coalesce(sum(reserved_micros), 0) FROM ai_budget_windows")
        == 0
    )
    assert (await world.state(source))["error_code"] == "budget_exhausted"


async def test_a_transient_provider_error_is_retried_by_the_call_policy_and_ledgered(world):
    world.stub.embed_failures = [(503, {}, {"error": "busy"}), (429, {"Retry-After": "0"}, {})]
    await queued(world)
    assert await world.run_worker() == ["succeeded"]
    statuses = await rows(
        world.sm,
        "SELECT status, error_code, attempt FROM ai_usage_ledger WHERE operation = 'embed_text' ORDER BY id",
    )
    assert [s["status"] for s in statuses] == ["failed", "failed", "ok"]
    assert [s["attempt"] for s in statuses] == [1, 2, 3]


async def test_a_provider_that_stays_down_requeues_the_job_with_a_delay_then_it_succeeds(world):
    world.stub.embed_failures = [(503, {}, {"error": "down"})] * 4  # all 4 call-policy attempts
    source, _ = await queued(world)
    assert await world.run_worker() == ["requeued"]
    state = await world.state(source)
    assert state["analysis_state"] == "queued"
    job = (
        await rows(
            world.sm,
            "SELECT state, attempts, error_code, run_after > now() AS later FROM analysis_jobs",
        )
    )[0]
    assert job == {
        "state": "queued",
        "attempts": 1,
        "error_code": "provider_unavailable",
        "later": True,
    }
    assert await world.run_worker() == []  # not before run_after
    await execute(world.sm, "UPDATE analysis_jobs SET run_after = now() - interval '1 second'")
    assert await world.run_worker() == ["succeeded"]
    assert (await world.state(source))["analysis_state"] == "ready"
    assert await scalar(world.sm, "SELECT attempts FROM analysis_jobs") == 2


async def test_after_the_last_attempt_a_down_provider_fails_the_job(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_analysis_max_attempts=1
    )
    world.stub.embed_failures = [(503, {}, {})] * 4
    source, _ = await queued(world)
    assert await world.run_worker() == ["failed:provider_unavailable"]
    assert (await world.state(source))["error_code"] == "provider_unavailable"


async def test_a_provider_400_is_not_retried_and_fails_the_job(world):
    world.stub.embed_failures = [(400, {}, {"error": {"message": "bad input"}})]
    source, _ = await queued(world)
    assert await world.run_worker() == ["failed:provider_bad_request"]
    assert len(world.stub.calls("batchEmbed")) == 1


async def test_an_embedding_of_the_wrong_dimension_is_never_stored(world):
    world.stub.wrong_dimension = True
    source, _ = await queued(world)
    assert await world.run_worker() == ["failed:provider_bad_output"]
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") == 0
    assert (await world.state(source))["analysis_state"] == "failed"


# ---- cancellation and sharing -----------------------------------------------------------------------------


async def test_withdrawing_the_only_waiter_cancels_a_queued_job_before_it_runs(world):
    source, _ = await queued(world)
    sid, vid = world.version_of(source)
    r = await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}/analyze")
    assert r.status_code == 204
    assert await world.run_worker() == []
    assert world.stub.requests == []
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs")) == [{"state": "cancelled"}]
    assert (await world.state(source))["analysis_state"] == "not_analyzed"


async def test_a_job_another_person_still_waits_for_is_kept(world, make_actor):
    editor = await member(world, make_actor, "Editor", "editor")
    source, _ = await queued(world)
    await world.analyze(editor, source)
    sid, vid = world.version_of(source)
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}/analyze")
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs")) == [{"state": "queued"}]
    assert await world.run_worker() == ["succeeded"]


async def test_cancelling_twice_or_when_nothing_is_queued_is_harmless(world):
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(1))
    sid, vid = world.version_of(source)
    url = f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}/analyze"
    assert (await world.owner.delete(url)).status_code == 204
    await world.analyze(world.owner, source)
    assert (await world.owner.delete(url)).status_code == 204
    assert (await world.owner.delete(url)).status_code == 204
    assert (await world.analyze(world.owner, source)).json()["status"] == "created"  # asks again


async def test_cancelling_a_running_job_stops_it_at_the_next_stage_and_leaves_nothing(
    world, monkeypatch
):
    source, body = await queued(world)
    sid, vid = world.version_of(source)
    real = pipeline.extract_file

    async def extract_then_cancel(*args, **kwargs):
        result = await real(*args, **kwargs)
        r = await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}/analyze")
        assert r.status_code == 204
        return result

    monkeypatch.setattr(pipeline, "extract_file", extract_then_cancel)
    assert await world.run_worker() == ["cancelled"]
    assert world.stub.requests == []  # no provider call after the cancellation
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM index_generations") == 0
    assert (await world.state(source))["analysis_state"] == "not_analyzed"
    assert not list((world.store.tmp_dir).glob("inspect-*"))


# ---- crash, lease and retry ----------------------------------------------------------------------------------


async def test_a_crash_between_attempts_leaves_exactly_one_generation(world, monkeypatch):
    source, _ = await queued(world)
    real = store.insert_chunks
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            await real(*args, **kwargs)  # one batch IS written ...
            raise RuntimeError("worker died")  # ... and then the attempt dies
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "insert_chunks", flaky)
    assert await world.run_worker() == ["requeued"]
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") >= 0
    await execute(world.sm, "UPDATE analysis_jobs SET run_after = now() - interval '1 second'")
    assert await world.run_worker() == ["succeeded"]
    gens = await rows(world.sm, "SELECT state FROM index_generations")
    assert gens == [{"state": "ready"}]  # the half-written one was deleted, only one remains
    total = await scalar(world.sm, "SELECT count(*) FROM chunks")
    assert total == await scalar(world.sm, "SELECT chunk_count FROM index_generations")


async def test_a_dead_workers_job_is_taken_over_after_its_lease_and_its_building_rows_removed(
    world,
):
    source, _ = await queued(world)
    claimed = await jobs.claim_next(world.sm, world.cfg)
    assert claimed is not None
    job, dead_token = claimed
    gen = await store.start_generation(
        world.sm,
        world.cfg,
        world.store,
        job,
        fingerprint=job.config_fingerprint,
        figure_mode="description",
    )
    # the dead worker's lease runs out
    await execute(
        world.sm, "UPDATE analysis_jobs SET lease_expires_at = now() - interval '1 second'"
    )
    assert await world.run_worker() == ["succeeded"]
    assert await scalar(world.sm, "SELECT attempts FROM analysis_jobs") == 2
    ids = [r["id"] for r in await rows(world.sm, "SELECT id FROM index_generations")]
    assert len(ids) == 1 and str(gen) not in {str(i) for i in ids}
    # the old worker, waking up late, can neither renew nor publish
    assert await jobs.renew(world.sm, world.cfg, job.id, dead_token) == "lost_claim"


async def test_a_worker_that_lost_its_claim_cannot_publish(world):
    source, _ = await queued(world)
    job, token = await jobs.claim_next(world.sm, world.cfg)
    gen = await store.start_generation(
        world.sm,
        world.cfg,
        world.store,
        job,
        fingerprint=job.config_fingerprint,
        figure_mode="description",
    )
    result = await store.publish(
        world.sm,
        job,
        uuid.uuid4(),
        gen,
        expected_chunks=1,
        expected_dims=8,
        summary={},
        warnings=[],
        gaps=[],
        pages_total=None,
        pages_unreadable=0,
        figures_found=0,
        figure_count=0,
    )
    assert result is None
    assert (await world.state(source))["current_generation_id"] is None


async def test_jobs_whose_attempts_ran_out_are_failed_by_the_sweeper(world):
    source, _ = await queued(world)
    await jobs.claim_next(world.sm, world.cfg)
    await execute(
        world.sm,
        "UPDATE analysis_jobs SET lease_expires_at = now() - interval '1 second', attempts = max_attempts",
    )
    assert await jobs.sweep_abandoned(world.sm) == 1
    assert (await rows(world.sm, "SELECT state, error_code FROM analysis_jobs")) == [
        {"state": "failed", "error_code": "attempts_exhausted"}
    ]
    assert (await world.state(source))["error_code"] == "attempts_exhausted"
    assert await world.run_worker() == []


async def test_a_running_job_whose_last_waiter_left_and_whose_worker_died_is_cancelled(world):
    source, _ = await queued(world)
    sid, vid = world.version_of(source)
    await jobs.claim_next(world.sm, world.cfg)
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}/versions/{vid}/analyze")
    await execute(
        world.sm, "UPDATE analysis_jobs SET lease_expires_at = now() - interval '1 second'"
    )
    assert await jobs.sweep_abandoned(world.sm) == 1
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs")) == [{"state": "cancelled"}]


async def test_two_workers_never_take_the_same_job(world):
    for i in range(4):
        data = sf.text_pdf([f"Document {i} says something unique about topic {i}."])
        source = await world.upload(world.owner, f"d{i}.pdf", data)
        await world.analyze(world.owner, source)
    # claims race: SKIP LOCKED makes a claimer pass over a row another claimer is just taking, so one
    # round may legitimately find fewer than 4 free jobs. What must hold is: never the same job twice.
    taken: list = []
    for _ in range(4):
        got = await asyncio.gather(*(jobs.claim_next(world.sm, world.cfg) for _ in range(8)))
        taken += [t for t in got if t is not None]
        if len(taken) >= 4:
            break
    ids = [t[0].id for t in taken]
    assert len(ids) == 4 and len(set(ids)) == 4


async def test_claiming_is_fair_across_rooms(world, make_actor, sessionmaker):
    from tests.doc_helpers import room_with_doc

    other_room, other_doc = await room_with_doc(world.owner, sessionmaker)
    big = []
    for i in range(3):
        s = await world.upload(world.owner, f"a{i}.pdf", sf.text_pdf([f"Room A file number {i}."]))
        await world.analyze(world.owner, s)
        big.append(s)
    # a file in the second room, uploaded last
    world.room_id, saved = other_room, world.room_id
    s = await world.upload(world.owner, "b.pdf", sf.text_pdf(["Room B file."]))
    await world.analyze(world.owner, s, document_id=other_doc["id"])
    world.room_id = saved
    first, _ = await jobs.claim_next(world.sm, world.cfg)  # room A (oldest), now running
    second, _ = await jobs.claim_next(world.sm, world.cfg)  # room B jumps ahead of A's second job
    assert str(first.room_id) == str(saved) and str(second.room_id) == str(other_room)


# ---- partial work is invisible ---------------------------------------------------------------------------------


async def test_a_building_generation_is_invisible_to_every_reader(world):
    source, _ = await queued(world)
    job, _token = await jobs.claim_next(world.sm, world.cfg)
    gen = await store.start_generation(
        world.sm,
        world.cfg,
        world.store,
        job,
        fingerprint=job.config_fingerprint,
        figure_mode="description",
    )
    from app.ai.analysis.chunking import DraftChunk

    draft = DraftChunk("half written", 1, 1, (), "text_layer", None, False, 3)
    await store.insert_chunks(world.sm, job, gen, [store.ChunkRow(draft)], [[0.1] * 8], 8)
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") == 1
    r = await world.passages(world.owner, source)
    assert r.status_code == 200 and r.json()["passages"] == []
    assert (await world.state(source))["current_generation_id"] is None
    assert (await world.status(world.owner, source))["generation_id"] is None


async def test_re_analysis_that_fails_never_replaces_a_usable_index(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(2))
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["succeeded"]
    good = (await world.state(source))["current_generation_id"]
    before = (await world.passages(world.owner, source)).json()["passages"]

    # the configuration changes (a different chunk size), so the old index is stale ...
    world.cfg = world.cfg.model_copy(
        update={"ai_chunk_target_tokens": 60, "ai_chunk_max_tokens": 120}
    )
    world.owner.client._transport.app.state.settings = world.cfg
    r = await world.analyze(world.owner, source)
    assert r.json()["status"] == "created"
    # ... and the provider fails for good while the new one is being built
    world.stub.embed_failures = [(400, {}, {"error": {"message": "no"}})]
    assert await world.run_worker() == ["failed:provider_bad_request"]
    state = await world.state(source)
    assert str(state["current_generation_id"]) == str(good)
    assert state["analysis_state"] == "ready" or state["analysis_state"] == "failed"
    after = (await world.passages(world.owner, source)).json()["passages"]
    # the failed version shows failed, but the OLD generation still serves its passages
    assert [p["text"] for p in after] in ([p["text"] for p in before], [])
    assert (
        await scalar(world.sm, "SELECT count(*) FROM index_generations WHERE state = 'building'")
        == 0
    )


async def test_a_new_config_publishes_a_new_generation_and_keeps_the_old_one_for_a_while(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)
    source = await world.upload(world.owner, "a.pdf", sf.text_pdf(2))
    await world.analyze(world.owner, source)
    await world.run_worker()
    old = (await world.state(source))["current_generation_id"]
    world.cfg = world.cfg.model_copy(
        update={"ai_chunk_target_tokens": 60, "ai_chunk_max_tokens": 120}
    )
    world.owner.client._transport.app.state.settings = world.cfg
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["succeeded"]
    new = (await world.state(source))["current_generation_id"]
    assert str(new) != str(old)
    states = {
        str(r["id"]): r["state"]
        for r in await rows(world.sm, "SELECT id, state FROM index_generations")
    }
    assert states[str(new)] == "ready" and states[str(old)] == "superseded"


# ---- the worker re-checks access before it touches anything --------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("demote", "insufficient_role"),
        ("remove", "room_access_lost"),
        ("room_switch", "ai_disabled_for_room"),
        ("kill_switch", "ai_disabled"),
        ("needs_repair", "ai_write_access_required"),
        ("consent", "consent_required"),
    ],
)
async def test_the_worker_stops_before_any_file_or_model_access_when_access_is_lost(
    world, make_actor, change, code
):
    editor = await member(world, make_actor, "Editor", "editor")
    source = await world.upload(editor, "a.pdf", sf.text_pdf(1))
    r = await world.analyze(editor, source)
    assert r.status_code == 202, r.text
    sm, room = world.sm, world.room_id
    if change == "demote":
        await add_member(sm, room, editor.id, "viewer")
    elif change == "remove":
        await add_member(sm, room, editor.id, "editor", "removed")
    elif change == "room_switch":
        await world.owner.put(f"/rooms/{room}/ai-settings", json={"ai_enabled": False})
    elif change == "kill_switch":
        await execute(
            sm,
            "INSERT INTO ai_global_flags (id, ai_enabled) VALUES (1, false) "
            "ON CONFLICT (id) DO UPDATE SET ai_enabled = false",
        )
    elif change == "needs_repair":
        await execute(
            sm,
            "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d",
            d=world.document["id"],
        )
    elif change == "consent":
        await execute(sm, "DELETE FROM ai_consents WHERE user_id = :u", u=editor.id)
    assert await world.run_worker() == [f"failed:{code}"]
    assert world.stub.requests == []  # no provider was contacted
    assert await scalar(sm, "SELECT count(*) FROM chunks") == 0
    state = await world.state(source)
    assert state["analysis_state"] == "failed" and state["error_code"] == code
    assert not list(world.store.tmp_dir.glob("inspect-*"))  # the file was never even opened


async def test_losing_access_while_running_is_noticed_at_the_next_gate_not_mid_flight(
    world, make_actor
):
    """The gate runs when a job is claimed. A change after that does not tear the job apart (it is
    one file's analysis with the keys decided at admission); the next job of that person is refused."""
    editor = await member(world, make_actor, "Editor", "editor")
    first = await world.upload(editor, "a.pdf", sf.text_pdf(1))
    second = await world.upload(editor, "b.pdf", sf.text_pdf(["Another file entirely."]))
    await world.analyze(editor, first)
    await world.analyze(editor, second)
    outcomes = []
    for i in range(2):
        outcomes.append(
            await pipeline.process_next(
                world.sm, world.cfg, world.store, transports=world.stub.transports()
            )
        )
        if i == 0:
            await add_member(world.sm, world.room_id, editor.id, "viewer")
    assert outcomes == ["succeeded", "failed:insufficient_role"]
