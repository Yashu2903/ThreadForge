"""`@AI` requests end to end: admission, waiting for files, execution, the published reply, events.

Real PostgreSQL (pgvector), real storage, the real extractor; the providers are HTTP stubs. The
worker is driven by `World.drain()`, which does what the AI worker loop does."""

import pytest

from tests import analysis_files as af
from tests.analysis_helpers import answer, block, build_world, diagram
from tests.helpers import rows, scalar

OPS = (
    b"# Operations\n\nThe warehouse processed twelve thousand orders in March. Delivery drivers were "
    b"short, so deliveries slipped.\n\n# Finance\n\nQuarterly revenue growth beat the budget.\n"
)


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def ready_source(world, name="ops.md", data=OPS):
    source = await world.upload(world.owner, name, data)
    assert (await world.analyze(world.owner, source)).status_code == 202
    assert set(await world.run_worker()) == {"succeeded"}
    return source


async def state_of(world, request_id):
    return (
        await rows(
            world.sm,
            "SELECT state, error_code, task_type FROM ai_requests WHERE id = :i",
            i=request_id,
        )
    )[0]


# ---- the happy path ----------------------------------------------------------------------------------------


async def test_a_sources_only_question_is_answered_with_a_cited_reply(world):
    source = await ready_source(world)
    r = await world.send(
        world.owner, "How many orders did the warehouse process?", sources=[source], task_hint="ask"
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["state"] == "queued" and created["queue_position"] == 1
    # the chat message exists at once, as an AI request message
    msg = (
        await rows(
            world.sm,
            "SELECT kind, body, ai_request_id, author_id FROM messages WHERE id = :m",
            m=created["message_id"],
        )
    )[0]
    assert (
        msg["kind"] == "ai_request"
        and msg["body"].startswith("@AI How many orders")
        and str(msg["ai_request_id"]) == created["id"]
    )
    assert msg["author_id"] == world.owner.id

    assert await world.drain() == ["request:published"]
    done = (await world.request(world.owner, created["id"])).json()
    assert done["state"] == "succeeded" and done["task_type"] == "qa" and done["reply_message_id"]

    reply = (
        await rows(
            world.sm,
            "SELECT kind, body, author_id, ai_request_id FROM messages WHERE id = :m",
            m=done["reply_message_id"],
        )
    )[0]
    assert reply["kind"] == "ai_answer" and str(reply["ai_request_id"]) == created["id"]
    assert "The evidence covers the request." in reply["body"] and "[1]" in reply["body"]
    assert (
        await scalar(world.sm, "SELECT display_name FROM users WHERE id = :u", u=reply["author_id"])
    ) == "ThreadForge AI"
    # the system author is not a member of anything
    assert (
        await scalar(
            world.sm, "SELECT count(*) FROM memberships WHERE user_id = :u", u=reply["author_id"]
        )
        == 0
    )

    result = (await world.request(world.owner, created["id"], "/result")).json()
    assert result["task_type"] == "qa" and not result["redacted"]
    cite = result["citations"][0]
    assert cite["label"] == "P1" and cite["source_name"] == "ops.md" and cite["excerpt"]
    assert result["coverage"]["sources"][0]["mode"] == "retrieval"
    assert result["answer"]["blocks"][0]["citations"] == [1]


async def test_files_that_are_not_analyzed_yet_are_analyzed_first_then_the_request_runs(world):
    source = await world.upload(world.owner, "ops.md", OPS)
    r = await world.send(world.owner, "Summarize the warehouse numbers", sources=[source])
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "waiting_for_sources" and body["stage_label"] == "Waiting for files"
    assert [s["analysis_state"] for s in body["sources"]] == ["queued"]
    assert world.stub.requests == []  # admission calls no provider
    assert await scalar(world.sm, "SELECT count(*) FROM analysis_jobs") == 1
    assert await scalar(world.sm, "SELECT kind FROM analysis_job_waiters") == "request"

    outcomes = await world.drain()
    assert outcomes == ["analysis:succeeded", "request:published"]
    assert (await state_of(world, body["id"]))["state"] == "succeeded"
    pinned = await rows(world.sm, "SELECT generation_id FROM ai_request_sources")
    assert pinned[0]["generation_id"] is not None  # frozen once ready


async def test_a_request_with_a_ready_index_makes_no_analysis_provider_calls(world):
    source = await ready_source(world)
    before = len(world.stub.calls("batchEmbed"))
    await world.send(world.owner, "How many orders?", sources=[source], task_hint="ask")
    await world.drain()
    # the only embedding call is the QUERY; nothing is re-embedded
    new_calls = world.stub.calls("batchEmbed")[before:]
    assert len(new_calls) == 1
    assert "How many orders?" in new_calls[0]["json"]["requests"][0]["content"]["parts"][0]["text"]


async def test_the_reply_and_request_events_reach_the_room_log_in_order(world):
    source = await ready_source(world)
    await world.send(world.owner, "How many orders?", sources=[source], task_hint="ask")
    await world.drain()
    events = await rows(
        world.sm,
        "SELECT type, resource_type, payload FROM room_events WHERE type IN ('ai_request_updated', 'message_created', 'source_updated') ORDER BY seq",
    )
    kinds = [(e["type"], (e["payload"] or {}).get("state")) for e in events]
    assert kinds[0] == ("source_updated", "created")
    assert ("source_updated", "ready") in kinds
    ai = [state for typ, state in kinds if typ == "ai_request_updated"]
    assert ai == ["queued", "running", "succeeded"]
    assert [t for t, _ in kinds].count("message_created") == 2  # the @AI message and the reply
    for e in events:
        assert "How many" not in str(e["payload"]) and "orders" not in str(
            e["payload"]
        )  # ids and states only


async def test_no_matching_evidence_is_said_plainly_and_the_model_is_not_asked_to_invent(world):
    source = await ready_source(world)
    await world.send(world.owner, "kubernetes cluster pods node", sources=[source], task_hint="ask")
    answers_before = world.stub.vision_calls("cited_answer")
    await world.drain()
    assert world.stub.vision_calls("cited_answer") == answers_before == 0
    done = (await rows(world.sm, "SELECT answer FROM ai_results"))[0]["answer"]
    assert "could not find anything relevant" in done["blocks"][0]["text"]
    assert done["blocks"][0]["note"] == "no_evidence"


# ---- classification --------------------------------------------------------------------------------------------


async def test_auto_classification_sees_only_the_instruction_and_can_pick_a_strategy(world):
    source = await ready_source(world)
    world.stub.classify_as = "summarize"
    r = await world.send(world.owner, "Give me the gist of this", sources=[source])
    await world.drain()
    calls = [
        c
        for c in world.stub.calls("/v1/responses")
        if (c["json"].get("text") or {}).get("format", {}).get("name") == "task_kind"
    ]
    assert len(calls) == 1
    sent = str(calls[0]["json"]["input"])
    assert (
        "Give me the gist" in sent and "warehouse" not in sent
    )  # no evidence reaches the classifier
    assert (await state_of(world, r.json()["id"]))["task_type"] == "summarize"


@pytest.mark.parametrize(
    ("classified", "sources", "expected"),
    [
        ("compare", 1, "qa"),
        ("document_review", 1, "qa"),
        ("summarize", 1, "summarize"),
        (None, 1, "qa"),
    ],
)
async def test_classification_can_never_widen_scope_and_a_failure_means_qa(
    world, classified, sources, expected
):
    source = await ready_source(world)
    world.stub.classify_as = classified
    r = await world.send(world.owner, "Compare or review things", sources=[source])
    await world.drain()
    assert (await state_of(world, r.json()["id"]))["task_type"] == expected


# ---- diagrams ----------------------------------------------------------------------------------------------------


async def test_a_valid_diagram_is_published_and_checked(world):
    source = await ready_source(world)
    world.stub.answer_script = [
        answer(block("The flow.", "P1"), diagram("flowchart TD\n  A[Orders] --> B[Delivery]", "P1"))
    ]
    r = await world.send(
        world.owner, "Draw a flowchart of the process", sources=[source], task_hint="ask"
    )
    await world.drain()
    result = (await world.request(world.owner, r.json()["id"], "/result")).json()
    d = result["answer"]["blocks"][1]
    assert (
        d["type"] == "diagram"
        and d["render_status"] in ("ok", "unchecked")
        and d["render_error"] is None
    )
    assert (
        "[Diagram: Flow]"
        in (await rows(world.sm, "SELECT body FROM messages WHERE kind = 'ai_answer'"))[0]["body"]
    )


# ---- the document ------------------------------------------------------------------------------------------------------


async def test_a_sources_only_request_never_sends_document_text_or_chat(world):
    source = await ready_source(world)
    await world.send(world.owner, "How many orders?", sources=[source], task_hint="ask")
    await world.drain()
    sent = [
        c
        for c in world.stub.calls("/v1/responses")
        if (c["json"].get("text") or {}).get("format", {}).get("name") == "cited_answer"
    ]
    prompt = str(sent[0]["json"]["input"])
    assert "THE USER'S OPEN DOCUMENT" not in prompt and "CHAT MESSAGES" not in prompt
    assert not sent[0]["json"].get("tools")  # the model gets no tools
    manifest = (await rows(world.sm, "SELECT data FROM ai_manifests"))[0]["data"]
    assert manifest["document"] is None and manifest["chat_messages"] == []
    assert af  # (module imported for the shared fixtures)
