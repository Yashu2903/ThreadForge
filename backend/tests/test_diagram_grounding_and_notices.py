"""Diagrams that say what they assume (A6, A7, P4, P8 of the review sheet) and requests that say what they lack.

THE FINDING. The release notes name two steps and an owner for each, and say there are three gates. They never say that
Build comes before Deploy. A diagram that draws `Build --> Deploy` states an order the source does not establish, and a
reader who sees only the picture cannot tell. The contract now: a diagram block lists the relations it draws that the
sources do not state (`assumptions`); the answer card, the chat message and the proposal preview show them, and a proposal
also gets a short italic note after the diagram so the caveat travels into the document.

What these tests cover is the PLUMBING: that assumptions are read, bounded, shown and kept out of the claim counts. Whether a
real model discloses an assumption when it should is measured in the quality evaluation, separately, and the Mermaid syntax
check stays a separate result from the grounding check.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.answer import mermaid
from app.ai.answer import prompts as answer_prompts
from app.ai.answer.answer import (
    MAX_ASSUMPTIONS,
    ClaimStats,
    ValidatedAnswer,
    read_assumptions,
    render_plain_text,
)
from app.ai.edit import check as proposal_check
from app.ai.edit import prompts as edit_prompts
from app.ai.edit.schema import ASSUMPTION_NOTE
from tests.ai_request_helpers import FakeCollab
from tests.analysis_helpers import (
    World,
    answer,
    block,
    build_world,
    diagram,
    edit_reply,
    new_block,
    op,
)
from tests.doc_helpers import new_room
from tests.helpers import rows

RELEASE = (
    b"# Release process\n\nEvery release goes through three gates.\n\n## Build\n\n- compile the sources\n"
    b"- run the unit tests\n\n## Deploy\n\n| Stage | Owner |\n|---|---|\n| Build | Alice |\n| Deploy | Bob |\n\n"
    b"The deploy step restarts the workers one at a time.\n"
)
ORDER = "Build is drawn before Deploy; the source does not say which comes first."
FLOW = "flowchart TD\n  A[Build] --> B[Deploy]"
NODE = pytest.mark.skipif(
    not mermaid.default_script().is_file(), reason="run `npm run build` first"
)
NODE_CHECK = pytest.mark.skipif(
    not proposal_check.default_script().is_file(), reason="run `npm run build` first"
)


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_proposals_enabled=True
    )


async def ready(world, name="release.md", data=RELEASE):
    source = await world.upload(world.owner, name, data)
    await world.analyze(world.owner, source)
    assert set(await world.run_worker()) == {"succeeded"}
    return source


# ---- the plumbing ---------------------------------------------------------------------------------------------------


def test_assumptions_are_cleaned_deduplicated_and_bounded():
    got = read_assumptions(
        {
            "assumptions": [
                "  Build   comes\nbefore Deploy. ",
                "Build comes before Deploy.",
                "",
                7,
                None,
                *[f"Assumption number {i}." for i in range(20)],
                "x" * 900,
            ]
        }
    )
    assert got[0] == "Build comes before Deploy."
    assert len(got) == MAX_ASSUMPTIONS and len(set(got)) == len(got)
    assert read_assumptions({"assumptions": None}) == [] and read_assumptions({}) == []
    assert read_assumptions({"assumptions": "not a list"}) == []
    assert all(len(a) <= 240 for a in read_assumptions({"assumptions": ["x" * 900]}))


def test_the_chat_text_of_a_diagram_carries_its_assumptions_and_the_server_notes_survive_a_cut():
    v = ValidatedAnswer(
        blocks=[
            {"type": "paragraph", "text": "word " * 2000, "citations": [1], "unsupported": False},
            {
                "type": "diagram",
                "title": "Flow",
                "mermaid": FLOW,
                "assumptions": [ORDER],
                "citations": [1],
            },
        ],
        citations=[],
        evidence_gaps=[],
        stats=ClaimStats(),
        diagrams=[1],
    )
    text = render_plain_text(
        v, 600, "Open the answer card.", notes=["The open document was not included."]
    )
    assert text.startswith("Note: The open document was not included.")
    assert len(text) <= 600 and text.endswith("Open the answer card.")
    short = render_plain_text(ValidatedAnswer([v.blocks[1]], [], [], ClaimStats(), [0]), 4000, "")
    assert "[Diagram: Flow]" in short and f"Assumed, not stated in the sources: {ORDER}" in short


def test_several_assumptions_are_joined_without_doubled_full_stops():
    v = ValidatedAnswer(
        blocks=[
            {
                "type": "diagram",
                "title": "Flow",
                "mermaid": FLOW,
                "assumptions": ["Build is drawn first.", "Deploy follows Build."],
                "citations": [],
            }
        ],
        citations=[],
        evidence_gaps=[],
        stats=ClaimStats(),
        diagrams=[0],
    )
    text = render_plain_text(v, 4000, "")
    assert "Assumed, not stated in the sources: Build is drawn first; Deploy follows Build." in text
    assert ".." not in text


def test_both_system_prompts_carry_the_grounding_rule_and_the_review_prompt_separates_effect_from_gap():
    for system in (
        answer_prompts.system_prompt("qa"),
        edit_prompts.edit_system(),
        edit_prompts.draft_system(),
    ):
        assert "GROUNDING OF DIAGRAMS" in system
        assert "Never invent a step" in system and "assumptions" in system
    review = answer_prompts.system_prompt("document_review")
    assert "could slip the schedule" in review and "IS a stated effect" in review
    assert "Beyond the document:" in review and "Do not invent risks" in review
    assert answer_prompts.PROMPT_TEMPLATE_VERSION == "answer-v2"


# ---- an answer ---------------------------------------------------------------------------------------------------


@NODE
async def test_an_answer_diagram_publishes_its_assumptions_in_the_card_and_the_chat_message(world):
    source = await ready(world)
    world.stub.answer_fn = lambda p: answer(
        {**diagram(FLOW, *p.labels[:2], title="Release"), "assumptions": [ORDER]},
        block("Alice owns Build and Bob owns Deploy.", *p.labels[:1]),
        gaps=("The sources name two of the three gates.",),
    )
    r = await world.send(
        world.owner, "Draw a flowchart of build and deploy.", sources=[source], task_hint="ask"
    )
    assert (await world.drain())[-1] == "request:published"
    rid = r.json()["id"]
    result = (await world.request(world.owner, rid, "/result")).json()
    d = next(b for b in result["answer"]["blocks"] if b["type"] == "diagram")
    assert d["assumptions"] == [ORDER] and d["render_status"] == "ok"
    # the syntax check and the grounding disclosure are separate facts
    assert result["answer"]["evidence_gaps"] == ["The sources name two of the three gates."]
    [message] = await rows(world.sm, "SELECT body FROM messages WHERE kind = 'ai_answer'")
    assert f"Assumed, not stated in the sources: {ORDER}" in message["body"]
    assert "Not found in the sources: The sources name two of the three gates." in message["body"]


# ---- a proposal ----------------------------------------------------------------------------------------------------


def collab(world) -> FakeCollab:
    return FakeCollab(world.document["id"], world.cfg.internal_api_secret.get_secret_value())


def diagram_op(*, assumptions):
    return op(
        "insert_blocks_after",
        anchor="B3",
        refs=("P1",),
        blocks=[
            new_block(
                "diagram",
                mermaid=FLOW,
                assumptions=assumptions,
                citations=[{"ref": "P1", "quote": None}],
            )
        ],
        reason="The documented steps.",
    )


async def edit_request(world, fc):
    return await world.send(
        world.owner,
        "Draw a flowchart of the build and deploy steps.",
        sources=[await ready(world)],
        operation="edit",
        document_id=world.document["id"],
        include_document_content=True,
        document_scope="edit",
        task_hint="ask",
        selected_block_ids=fc.block_ids(2),
        selection_hash=fc.hash_for(2),
    )


@NODE
@NODE_CHECK
async def test_a_proposal_diagram_with_assumptions_gets_an_italic_note_after_it_and_a_preview_entry(
    world,
):
    fc = collab(world)
    world.stub.edit_script = [edit_reply(diagram_op(assumptions=[ORDER]), summary="A flowchart.")]
    r = await edit_request(world, fc)
    assert r.status_code == 201, r.text
    assert (await world.drain(collab=fc.transport()))[-1] == "request:published"
    [prop] = await rows(world.sm, "SELECT * FROM ai_proposals")
    [change] = prop["preview"]["changes"]
    kinds = [n["type"] for n in change["after"]]
    assert kinds == [
        "diagram",
        "paragraph",
    ]  # the server-made note follows the diagram it qualifies
    note = change["after"][1]
    assert note["content"][0]["text"] == ASSUMPTION_NOTE + ORDER
    assert any(m["type"] == "italic" for m in note["content"][0].get("marks", []))
    assert change["assumptions"] == [ORDER]
    # the note is not a claim of the model: one operation, one claim, supported by the cited passage
    result = (await world.request(world.owner, r.json()["id"], "/result")).json()
    assert (
        result["answer"]["stats"]["claims"] == 1 and result["answer"]["stats"]["unsupported"] == 0
    )


@NODE
@NODE_CHECK
async def test_a_proposal_diagram_that_draws_only_stated_relations_gets_no_note(world):
    fc = collab(world)
    world.stub.edit_script = [edit_reply(diagram_op(assumptions=None), summary="A flowchart.")]
    await edit_request(world, fc)
    assert (await world.drain(collab=fc.transport()))[-1] == "request:published"
    [prop] = await rows(world.sm, "SELECT * FROM ai_proposals")
    [change] = prop["preview"]["changes"]
    assert [n["type"] for n in change["after"]] == ["diagram"] and change["assumptions"] == []


@NODE
@NODE_CHECK
async def test_a_draft_diagram_note_keeps_the_block_citations_aligned(world):
    from tests.analysis_helpers import draft_reply, para

    room = await new_room(world.owner)
    w2 = World(
        world.owner, room, {"id": str(uuid.uuid4())}, world.sm, world.store, world.cfg, world.stub
    )
    source = await w2.upload(world.owner, "release.md", RELEASE)
    world.stub.draft_script = [
        draft_reply(
            "Release flow",
            para("Intro paragraph.", "P1"),
            new_block(
                "diagram",
                mermaid=FLOW,
                assumptions=[ORDER],
                citations=[{"ref": "P1", "quote": None}],
            ),
            para("Closing paragraph.", "P1"),
        )
    ]
    r = await world.owner.post(
        f"/rooms/{room}/ai-requests",
        json={
            "client_request_id": str(uuid.uuid4()),
            "instruction": "Draft a release overview from this file.",
            "operation": "draft",
            "task_hint": "ask",
            "source_version_ids": [w2.version_of(source)[1]],
        },
    )
    assert r.status_code == 201, r.text
    assert "request:published" in await world.drain()
    [prop] = await rows(world.sm, "SELECT * FROM ai_proposals")
    preview = prop["preview"]
    types = [n["type"] for n in preview["document"]["content"]]
    assert types[:4] == ["paragraph", "diagram", "paragraph", "paragraph"]
    assert len(preview["block_citations"]) >= 4  # one entry per block, the note included
    assert preview["block_citations"][2] == []  # the note cites nothing: the server wrote it
    assert preview["block_assumptions"][1] == [ORDER]


# ---- a request that lacks the context its words ask for -------------------------------------------------------------


async def test_asking_about_the_document_without_including_it_says_so_and_does_not_widen_the_request(
    world,
):
    source = await ready(world)
    world.stub.classify_as = "document_review"
    seen = []

    def reply(prompt):
        seen.append(prompt)
        return answer(block("The release has three gates.", *prompt.labels[:1]))

    world.stub.answer_fn = reply
    r = await world.send(world.owner, "Review this document.", sources=[source], task_hint="auto")
    assert r.status_code == 201, r.text
    assert (await world.drain())[-1] == "request:published"
    rid = r.json()["id"]
    result = (await world.request(world.owner, rid, "/result")).json()
    [notice] = result["coverage"]["notices"]
    assert notice["code"] == "document_not_included" and "Use open document" in notice["action"]
    assert result["coverage"]["routing"]["route_adjusted_to_context"] is True
    # the request stayed what the person made it: files only, no document read, no capture call
    assert not any("THE USER'S OPEN DOCUMENT" in p.text for p in seen)
    assert result["coverage"].get("document") is None
    [message] = await rows(world.sm, "SELECT body FROM messages WHERE kind = 'ai_answer'")
    assert message["body"].startswith("Note: You asked about the open document")


async def test_no_notice_when_the_person_chose_the_task_or_included_the_document(world):
    source = await ready(world)
    world.stub.classify_as = "document_review"
    r = await world.send(world.owner, "Review this document.", sources=[source], task_hint="ask")
    assert (await world.drain())[-1] == "request:published"
    result = (await world.request(world.owner, r.json()["id"], "/result")).json()
    assert "notices" not in result["coverage"]  # an explicit choice is never second-guessed
