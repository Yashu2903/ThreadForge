"""Lazy analysis, end to end: the API asks, the worker (process_next) analyzes, retrieval-ready
passages appear. Real PostgreSQL with pgvector, real storage, the real sandboxed extractor and the
real Tesseract binary; only the two providers are HTTP-level stubs."""

import shutil

import pytest

from tests import analysis_files as af
from tests import source_files as sf
from tests.analysis_helpers import build_world
from tests.helpers import rows, scalar

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract missing")


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


# ---- upload analyzes nothing ----------------------------------------------------------------------------


async def test_upload_never_analyzes_or_calls_a_provider(world):
    source = await world.upload(world.owner, "report.pdf", sf.text_pdf(2))
    assert (await world.state(source))["analysis_state"] == "not_analyzed"
    assert world.stub.requests == []
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") == 0


# ---- the happy path ---------------------------------------------------------------------------------------


async def test_a_text_pdf_is_analyzed_into_searchable_passages(world):
    source = await world.upload(world.owner, "report.pdf", sf.text_pdf(3))
    r = await world.analyze(world.owner, source)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "created" and body["job_id"]
    assert (await world.state(source))["analysis_state"] == "queued"
    assert world.stub.requests == []  # the API call itself reaches no provider

    assert await world.run_worker() == ["succeeded"]
    state = await world.state(source)
    assert state["analysis_state"] == "ready" and state["current_generation_id"]

    status = await world.status(world.owner, source)
    assert status["analysis_state"] == "ready" and status["job_state"] == "succeeded"
    assert status["chunk_count"] >= 1 and status["pages_total"] == 3
    assert status["warnings"] == [] and status["gaps"] == []

    r = await world.passages(world.owner, source)
    assert r.status_code == 200
    passages = r.json()["passages"]
    assert passages and "quarterly report" in " ".join(p["text"] for p in passages)
    assert all(p["extraction"] == "text_layer" for p in passages)
    assert passages[0]["locator"]["page_start"] == 1

    # only the embedding provider was called: a text PDF needs no vision
    assert world.stub.vision_calls("page_transcription") == 0
    assert world.stub.calls("batchEmbedContents")
    ledger = await rows(
        world.sm, "SELECT operation, status, key_source, analysis_job_id FROM ai_usage_ledger"
    )
    assert {r["operation"] for r in ledger} == {"embed_text"}
    assert all(r["status"] == "ok" and r["analysis_job_id"] for r in ledger)


async def test_vectors_have_the_configured_dimension_and_pgvector_can_rank_them(world):
    source = await world.upload(world.owner, "ops.md", af.MARKDOWN_SAMPLE.encode())
    assert (await world.analyze(world.owner, source)).status_code == 202
    assert await world.run_worker() == ["succeeded"]
    dims = await rows(
        world.sm, "SELECT DISTINCT vector_dims(embedding) AS d, embedding_dims FROM chunks"
    )
    assert dims == [{"d": 8, "embedding_dims": 8}]
    nearest = await rows(
        world.sm,
        "SELECT text FROM chunks ORDER BY embedding <=> CAST(:q AS vector) LIMIT 1",
        q="[0,0,1,0,0,0,0,0]",  # the deployment topic
    )
    assert "deploy" in nearest[0]["text"].lower() or "release" in nearest[0]["text"].lower()


async def test_a_second_attach_reuses_the_ready_index_with_no_provider_call(world):
    source = await world.upload(world.owner, "report.pdf", sf.text_pdf(2))
    await world.analyze(world.owner, source)
    await world.run_worker()
    calls_before = world.stub.provider_call_count()
    ledger_before = await scalar(world.sm, "SELECT count(*) FROM ai_usage_ledger")

    r = await world.analyze(world.owner, source)
    assert r.status_code == 202 and r.json()["status"] == "ready" and r.json()["job_id"] is None
    assert await world.run_worker() == []  # nothing queued
    assert world.stub.provider_call_count() == calls_before
    assert await scalar(world.sm, "SELECT count(*) FROM ai_usage_ledger") == ledger_before
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 1


async def test_two_people_attaching_the_same_file_share_one_job(world, make_actor, sessionmaker):
    from tests.analysis_helpers import add_member, give_consent

    editor = await make_actor("Editor", app_settings=world.cfg)
    from tests.ai_helpers import install

    install(editor, world.stub)
    await add_member(sessionmaker, world.room_id, editor.id, "editor")
    await give_consent(editor)
    source = await world.upload(world.owner, "report.pdf", sf.text_pdf(2))
    first = await world.analyze(world.owner, source)
    second = await world.analyze(editor, source)
    assert first.json()["status"] == "created" and second.json()["status"] == "joined"
    assert first.json()["job_id"] == second.json()["job_id"]
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 1
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_job_waiters") == 2
    assert await world.run_worker() == ["succeeded"]
    # the first requester's key paid for the shared job
    assert (
        await scalar(world.sm, "SELECT DISTINCT charged_user_id FROM ai_usage_ledger")
        == world.owner.id
    )


async def test_docx_headings_tables_and_figure_are_indexed_with_locators(world):
    source = await world.upload(
        world.owner, "handbook.docx", af.rich_docx(figure=af.diagram_png(), sections=2)
    )
    assert (await world.analyze(world.owner, source)).status_code == 202
    assert await world.run_worker() == ["succeeded"]
    passages = (await world.passages(world.owner, source)).json()["passages"]
    text_chunks = [p for p in passages if p["kind"] == "text"]
    assert any(p["locator"]["heading_path"][:1] == ["Section 1 overview"] for p in text_chunks)
    table = next(p for p in text_chunks if "Stage | Owner" in p["text"])
    assert "Build | Alice" in table["text"]
    figures = [p for p in passages if p["kind"] == "figure"]
    assert len(figures) == 1 and figures[0]["extraction"] == "vision"
    assert figures[0]["figure_number"] == 1 and "Client" in figures[0]["text"]
    assert "Client -> API (request)" in figures[0]["text"]
    assert world.stub.vision_calls("figure_description") == 1
    # the figure image was stored, re-encoded, under an opaque key
    key = await scalar(world.sm, "SELECT storage_key FROM figures")
    assert world.store.exists("figures", key)


@needs_tesseract
async def test_a_clean_scan_is_read_locally_with_no_vision_call(world):
    source = await world.upload(world.owner, "scan.pdf", af.ocr_scan_pdf(1))
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["succeeded"]
    passages = (await world.passages(world.owner, source)).json()["passages"]
    assert passages[0]["extraction"] == "tesseract"
    assert passages[0]["ocr_confidence"] >= 60 and "warehouse" in passages[0]["text"].lower()
    assert world.stub.vision_calls("page_transcription") == 0


@needs_tesseract
async def test_a_bad_scan_falls_back_to_the_vision_model_and_is_cross_checked(world):
    world.stub.transcript = "Quarterly operations review. The warehouse processed orders in March."
    source = await world.upload(world.owner, "bad.pdf", af.noise_scan_pdf(1))
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["succeeded"]
    assert world.stub.vision_calls("page_transcription") == 1
    passages = (await world.passages(world.owner, source)).json()["passages"]
    assert passages[0]["extraction"] == "vision_ocr" and passages[0]["ocr_confidence"] is None
    ops = {r["operation"] for r in await rows(world.sm, "SELECT operation FROM ai_usage_ledger")}
    assert {"vision_ocr", "embed_text"} <= ops


@needs_tesseract
async def test_unreadable_pages_become_gaps_and_a_visible_warning(world):
    # one clean page and one unreadable page (the vision model also finds nothing legible)
    mixed = af.pdf_from_images(
        [af.page_image(af.CLEAN_SCAN_LINES)]
        + [
            af.Image.frombytes("RGB", (850, 1100), __import__("os").urandom(850 * 1100 * 3)).resize(
                (2550, 3300)
            )
        ]
        + [af.page_image(af.CLEAN_SCAN_LINES)]
        + [af.page_image(af.CLEAN_SCAN_LINES)]
    )
    world.stub.transcript = "[illegible] [illegible] [illegible]"
    source = await world.upload(world.owner, "mixed.pdf", mixed)
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["succeeded"]
    status = await world.status(world.owner, source)
    assert status["pages_total"] == 4 and status["pages_unreadable"] == 1
    assert status["gaps"] == [{"page": 2, "reason": "illegible"}]
    assert [w["code"] for w in status["warnings"]] == ["unreadable_pages"]


@needs_tesseract
async def test_more_than_30_percent_unreadable_pages_fails_the_version_never_ready(world):
    world.stub.transcript = "[illegible]"
    source = await world.upload(world.owner, "junk.pdf", af.noise_scan_pdf(2))
    await world.analyze(world.owner, source)
    assert await world.run_worker() == ["failed:too_many_unreadable_pages"]
    state = await world.state(source)
    assert (
        state["analysis_state"] == "failed" and state["error_code"] == "too_many_unreadable_pages"
    )
    assert state["current_generation_id"] is None
    assert await scalar(world.sm, "SELECT count(*) FROM chunks") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM index_generations") == 0
    status = await world.status(world.owner, source)
    assert (
        status["job_state"] == "failed" and status["job_error_code"] == "too_many_unreadable_pages"
    )
