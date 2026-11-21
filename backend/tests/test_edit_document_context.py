"""P7 of the human review sheet: "Add a closing summary paragraph" reached the model with NO document text.

edit-v1 showed the model only the blocks the person selected (plus a table of contents above 40 blocks). An insertion
at the end of the document selects nothing, so the model got an empty changeable list, no evidence and no document, and
answered with a placeholder sentence while its own summary said the content was not provided. edit-v2 adds the rest of
the document as READ-ONLY context inside the same untrusted-data markers, with the size decision recorded.

What these tests pin: the context reaches the model for exactly that request; it is read only (the scope the person
chose does not widen); a repair keeps the same context and instruction; a partial read is stated to the model and in
the coverage; a document the model could not see at all is refused instead of answered with a placeholder.
The model is a stand-in: nothing here says anything about how a real model summarizes.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.answer.capture import Captured, DocBlock
from app.ai.edit import check as proposal_check
from app.ai.edit import prompts
from app.ai.edit.target import ContextDecision, linearize, resolve_edit_target
from tests.ai_request_helpers import FakeCollab
from tests.analysis_helpers import World, build_world, edit_reply, op, para
from tests.helpers import rows

NODE = pytest.mark.skipif(
    not proposal_check.default_script().is_file(), reason="run `npm run build` first"
)

FACTS = [
    ("heading", 1, "Project Atlas brief"),
    (
        "paragraph",
        0,
        "Atlas moves the order system from the old data centre to the cloud by 30 June.",
    ),
    ("paragraph", 0, "The migration budget is 120,000 euros, approved by the steering group."),
    ("heading", 2, "Risks"),
    ("paragraph", 0, "The main risk is the payment gateway, which has no staging environment."),
    ("paragraph", 0, "Maria Keller owns the cutover plan; Tom Vogel owns the rollback plan."),
]


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix) -> World:
    return await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_proposals_enabled=True
    )


def collab_for(world, items=None) -> FakeCollab:
    return FakeCollab(world.document["id"], world.cfg.internal_api_secret.get_secret_value(), items)


def fields(world, **extra) -> dict:
    return {
        "operation": "edit",
        "document_id": world.document["id"],
        "include_document_content": True,
        "document_scope": "edit",
        "task_hint": "ask",
        "insert_at": "end",
        **extra,
    }


async def request_row(world, rid: str):
    [row] = await rows(world.sm, "SELECT * FROM ai_requests WHERE id = :i", i=uuid.UUID(rid))
    return row


def closing(prompt) -> dict:
    last = prompt.anchors[-1] if prompt.anchors else "start"
    return edit_reply(
        op(
            "insert_blocks_after",
            anchor=last,
            blocks=[para("Atlas moves the order system to the cloud by 30 June.")],
        ),
        summary="Added a closing summary.",
    )


# ---- the pure part: what the model is shown --------------------------------------------------------------


def captured_of(items) -> Captured:
    document_id = uuid.uuid4()
    blocks, nodes = [], []
    for i, (kind, level, text) in enumerate(items):
        bid = str(uuid.uuid5(document_id, str(i)))
        node = {"type": kind, "attrs": {"id": bid}, "content": [{"type": "text", "text": text}]}
        if kind == "heading":
            node["attrs"]["level"] = level
        nodes.append(node)
        blocks.append(DocBlock(bid, kind, level if kind == "heading" else 0, text, f"h{i}", None))
    return Captured(document_id, 3, "content-hash", blocks, {"type": "doc", "content": nodes})


def target_end(captured: Captured):
    return resolve_edit_target(
        captured, block_ids=[], selection_hash=None, ranges=[], insert_at="end"
    )


def test_the_historical_cause_without_context_the_model_sees_nothing_of_the_document():
    # edit-v1 behaviour, reproduced: an end insertion has no scope, so no line of the document reaches the prompt
    captured = captured_of(FACTS)
    view = linearize(captured, target_end(captured))  # no context budget: exactly what edit-v1 did
    from app.ai.answer.answer import EvidenceSet

    parts = prompts.edit_user_parts(
        "Add a closing summary paragraph.", EvidenceSet(), view, target_end(captured)
    )
    text = parts[0].text
    assert view.scope_lines == [] and view.context is None
    assert "payment gateway" not in text and "120,000" not in text and "Atlas" not in text
    assert "DOCUMENT CONTEXT" not in text


def test_with_a_budget_the_whole_small_document_is_shown_read_only_and_marked_complete():
    captured = captured_of(FACTS)
    target = target_end(captured)
    view = linearize(captured, target, context_budget_tokens=2000)
    assert view.context and view.context.mode == "full" and view.context.complete
    assert (view.context.blocks_total, view.context.blocks_shown) == (6, 6)
    joined = "\n".join(view.context_lines)
    for fact in ("30 June", "120,000 euros", "payment gateway", "Maria Keller"):
        assert fact in joined
    from app.ai.answer.answer import EvidenceSet

    text = prompts.edit_user_parts("Add a closing summary paragraph.", EvidenceSet(), view, target)[
        0
    ].text
    assert "DOCUMENT CONTEXT (READ ONLY" in text
    assert "COMPLETE: all 6 blocks" in text
    assert text.index("--- INSERTION POINTS ---") < text.index("--- DOCUMENT CONTEXT")


def test_context_lines_never_have_the_shape_of_a_changeable_block():
    captured = captured_of(FACTS)
    view = linearize(captured, target_end(captured), context_budget_tokens=2000)
    from tests.analysis_helpers import SCOPE_LINE

    assert view.scope_lines == []
    assert not any(SCOPE_LINE.match(line) for line in view.context_lines)
    assert all(line.startswith("(B") for line in view.context_lines)


def test_selected_blocks_are_not_repeated_in_the_context():
    captured = captured_of(FACTS)
    target = resolve_edit_target(
        captured,
        block_ids=[captured.blocks[2].id],
        selection_hash=None,
        ranges=[],
        insert_at=None,
        skip_hash=True,
    )
    view = linearize(captured, target, context_budget_tokens=2000)
    assert any("120,000 euros" in line for line in view.scope_lines)
    assert not any("120,000 euros" in line for line in view.context_lines)
    assert any("[changeable: shown in full above]" in line for line in view.context_lines)


def long_document(n: int = 40):
    items = [("heading", 1, "Long report")]
    for i in range(n):
        if i % 10 == 0:
            items.append(("heading", 2, f"Part {i // 10 + 1}"))
        items.append(("paragraph", 0, f"Point {i}: " + ("detail " * 200)))
    return captured_of(items)


def test_a_document_over_the_budget_is_clipped_then_sampled_and_says_so():
    captured = long_document()
    target = target_end(captured)
    measure = lambda t: max(1, len(t) // 4)  # noqa: E731
    clipped = linearize(captured, target, context_budget_tokens=6500, measure=measure).context
    assert clipped and clipped.mode == "clipped" and clipped.complete is False
    assert clipped.blocks_shown == clipped.blocks_total and clipped.clipped_blocks > 0

    view = linearize(captured, target, context_budget_tokens=900, measure=measure)
    c = view.context
    assert c and c.mode == "sampled" and not c.complete
    assert c.blocks_shown < c.blocks_total and c.omitted_blocks == c.blocks_total - c.blocks_shown
    assert c.tokens_shown <= 900
    joined = "\n".join(view.context_lines)
    assert "Long report" in joined and all(
        f"Part {n}" in joined for n in (1, 2, 3, 4)
    )  # every heading kept
    assert "Point 0:" in joined and "Point 39:" in joined  # the first and the last block kept
    assert "not shown" in joined  # every gap is marked
    status = prompts.context_status(view)
    assert status.startswith("PARTIAL:") and str(c.omitted_blocks) in status


def test_an_empty_document_has_no_context_and_says_so():
    captured = captured_of([("paragraph", 0, "")])
    view = linearize(captured, target_end(captured), context_budget_tokens=2000)
    assert view.context == ContextDecision("none", 0, 0, 0, 0, 2000)
    assert prompts.context_status(view) == "The document has no text yet."


def test_the_system_prompt_forbids_the_placeholder_and_keeps_the_scope_rule():
    system = prompts.edit_system()
    assert prompts.PROMPT_TEMPLATE_VERSION == "edit-v2"
    assert "READ ONLY" in system and "only the CHANGEABLE BLOCKS" in system.replace(
        "change only the ", "only the "
    )
    assert "Never write a placeholder" in system
    assert "evidence_gaps" in system


# ---- through the real request path --------------------------------------------------------------------------


@NODE
async def test_p7_reproduced_the_model_now_receives_the_document_for_the_same_request(world):
    fc = collab_for(world, list(FACTS))
    seen = []

    def reply(prompt):
        seen.append(prompt)
        return closing(prompt)

    world.stub.edit_fn = reply
    r = await world.send(world.owner, "Add a closing summary paragraph.", **fields(world))
    assert r.status_code == 201, r.text
    assert (await world.drain(collab=fc.transport()))[-1] == "request:published"
    [prompt] = seen
    for fact in ("30 June", "120,000 euros", "payment gateway", "Maria Keller", "Tom Vogel"):
        assert fact in prompt.text, fact
    assert prompt.scope == {}  # nothing is changeable: this is an insertion
    assert prompt.anchors  # and the only way to write is the offered insertion point
    assert "COMPLETE: all 6 blocks" in prompt.text
    # the person's instruction is the only TASK
    assert "Add a closing summary paragraph." in prompt.text.split("===== BEGIN UNTRUSTED")[0]


@NODE
async def test_the_coverage_and_manifest_state_what_was_read(world):
    fc = collab_for(world, list(FACTS))
    world.stub.edit_fn = closing
    r = await world.send(world.owner, "Add a closing summary paragraph.", **fields(world))
    rid = r.json()["id"]
    await world.drain(collab=fc.transport())
    result = (await world.request(world.owner, rid, "/result")).json()
    doc = result["coverage"]["document"]
    assert doc["mode"] == "target_plus_context"
    assert (
        doc["document_id"] == world.document["id"] and doc["captured_update_counter"] == fc.counter
    )
    assert doc["context"]["mode"] == "full" and doc["context"]["blocks_total"] == 6
    [manifest] = await rows(
        world.sm,
        "SELECT data FROM ai_manifests WHERE request_id = :r ORDER BY attempt DESC LIMIT 1",
        r=uuid.UUID(rid),
    )
    ctx = manifest["data"]["document"]["context"]
    assert ctx["blocks_shown"] == 6 and ctx["complete"] is True
    assert manifest["data"]["prompt_template"] == "edit-v2"
    assert result["coverage"]["validation"]["context"]["mode"] == "full"


@NODE
async def test_a_repair_keeps_the_same_context_and_the_original_instruction(world):
    fc = collab_for(world, list(FACTS))
    seen = []

    def reply(prompt):
        seen.append(prompt)
        # the first reply is rejected (no operations), the second is fine
        return edit_reply() if len(seen) == 1 else closing(prompt)

    world.stub.edit_fn = reply
    r = await world.send(world.owner, "Add a closing summary paragraph.", **fields(world))
    assert r.status_code == 201
    assert (await world.drain(collab=fc.transport()))[-1] == "request:published"
    assert len(seen) == 2
    first, second = seen
    assert "YOUR PREVIOUS REPLY WAS REJECTED" not in first.text
    assert "YOUR PREVIOUS REPLY WAS REJECTED" in second.text
    for p in (first, second):
        assert "Add a closing summary paragraph." in p.text
        for fact in ("30 June", "payment gateway", "Tom Vogel"):
            assert fact in p.text
        assert "DOCUMENT CONTEXT (READ ONLY" in p.text
    # the repaired call carries the same scope: same insertion points, same (empty) changeable blocks
    assert first.anchors == second.anchors and first.scope == second.scope


@NODE
async def test_the_context_does_not_widen_the_scope_a_context_block_cannot_be_changed(world):
    fc = collab_for(world, list(FACTS))
    attempts = []

    def hostile(prompt):
        attempts.append(prompt)
        # B3 is only context for this request (an end insertion selects nothing): changing it must be refused
        return edit_reply(
            op("replace_block_text", block="B3", new_inline=[{"text": "Changed.", "marks": []}])
        )

    world.stub.edit_fn = hostile
    r = await world.send(world.owner, "Add a closing summary paragraph.", **fields(world))
    rid = r.json()["id"]
    outcomes = await world.drain(collab=fc.transport())
    assert outcomes[-1].startswith("request:failed")
    row = await request_row(world, rid)
    assert row["state"] == "failed" and row["error_code"] == "proposal_invalid"
    assert len(attempts) == 2  # the one repair, not more
    assert not await rows(world.sm, "SELECT 1 FROM ai_proposals")


@NODE
async def test_a_large_document_is_partial_and_the_coverage_says_so(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor,
        sessionmaker,
        tmp_path,
        redis_prefix,
        ai_proposals_enabled=True,
        ai_max_input_tokens=6000,
    )
    items = [("heading", 1, "Long report")]
    for i in range(60):
        items.append(("paragraph", 0, f"Point {i}: " + ("detail " * 70)))
    fc = collab_for(world, items)
    seen = []

    def reply(prompt):
        seen.append(prompt)
        return closing(prompt)

    world.stub.edit_fn = reply
    r = await world.send(world.owner, "Add a closing summary paragraph.", **fields(world))
    rid = r.json()["id"]
    assert (await world.drain(collab=fc.transport()))[-1] == "request:published"
    assert "PARTIAL:" in seen[0].text and "not shown" in seen[0].text
    result = (await world.request(world.owner, rid, "/result")).json()
    doc = result["coverage"]["document"]
    assert doc["mode"] == "target_plus_partial_context"
    assert doc["context"]["complete"] is False and doc["context"]["omitted_blocks"] > 0


@NODE
async def test_a_document_the_model_would_not_see_at_all_is_refused_not_answered(
    world, monkeypatch
):
    from app.ai.answer import strategies

    real = strategies.budgets

    def no_document_room(settings, *, has_doc, has_chat):
        return {**real(settings, has_doc=has_doc, has_chat=has_chat), "document": 0}

    monkeypatch.setattr(strategies, "budgets", no_document_room)
    fc = collab_for(world, list(FACTS))
    called = []
    world.stub.edit_fn = lambda prompt: called.append(prompt) or closing(prompt)
    r = await world.send(world.owner, "Add a closing summary paragraph.", **fields(world))
    rid = r.json()["id"]
    outcomes = await world.drain(collab=fc.transport())
    assert outcomes[-1].startswith("request:failed")
    row = await request_row(world, rid)
    assert row["error_code"] == "document_context_unavailable"
    assert called == []  # no model call, nothing published
