"""The answer contract: labels, citations, quotes, support accounting, plain-text rendering, and the
Mermaid rules (shared cases with the TypeScript implementation)."""

import json
import uuid
from pathlib import Path

import pytest

from app.ai.answer import answer as ans
from app.ai.answer import mermaid
from tests.factories import make_settings

CASES = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "packages/editor-schema/test/fixtures/mermaid-cases.json"
    ).read_text(encoding="utf-8")
)
SCRIPT_BUILT = mermaid.default_script().is_file()


def evidence() -> ans.EvidenceSet:
    ev = ans.EvidenceSet()
    ev.add(
        "P",
        kind="passage",
        text="The  warehouse processed twelve thousand orders\nin March.",
        extraction="text_layer",
        source_version_id=uuid.uuid4(),
        source_name="ops.pdf",
        locator={"page_start": 3},
        chunk_ids=[uuid.uuid4()],
    )
    ev.add(
        "P",
        kind="passage",
        text="Scanned text about deliveries.",
        extraction="tesseract",
        locator={"page_start": 5},
    )
    ev.add(
        "P",
        kind="passage",
        text="A diagram of the API.",
        extraction="vision",
        is_figure=True,
        locator={"figure_number": 1},
    )
    ev.add("S", kind="section", text="Section summary text.", extraction="text_layer")
    ev.add("C", kind="chat", text="a chat message", extraction="plain")
    return ev


def paragraph(text="Orders grew.", citations=None):
    return {
        "type": "paragraph",
        "text": text,
        "items": None,
        "title": None,
        "mermaid": None,
        "citations": citations or [],
    }


def raw(*blocks, gaps=()):
    return {"blocks": list(blocks), "evidence_gaps": list(gaps)}


def cite(ref, quote=None):
    return {"ref": ref, "quote": quote}


# ---- citations and quotes ------------------------------------------------------------------------------------


def test_labels_are_request_local_and_numbered_in_order():
    ev = evidence()
    assert list(ev.items) == ["P1", "P2", "P3", "S1", "C1"]


def test_a_valid_citation_is_numbered_and_carries_a_short_excerpt():
    ev = evidence()
    out = ans.validate_answer(raw(paragraph(citations=[cite("P1")])), ev)
    assert out.blocks[0]["citations"] == [1] and not out.blocks[0]["unsupported"]
    row = out.citations[0]
    assert (
        row["label"] == "P1"
        and row["locator"] == {"page_start": 3}
        and row["source_name"] == "ops.pdf"
    )
    assert len(row["excerpt"]) <= ans.EXCERPT_LIMIT and row["machine_generated"] is False
    assert out.stats.claims == 1 and out.stats.unsupported == 0


def test_the_same_passage_cited_twice_is_one_row_in_the_table():
    out = ans.validate_answer(
        raw(paragraph("a", [cite("P1")]), paragraph("b", [cite("P1"), cite("P1")])), evidence()
    )
    assert len(out.citations) == 1 and out.blocks[1]["citations"] == [1]


@pytest.mark.parametrize("ref", ["P9", "X1", "", "p1", "P01", "P1; DROP", "../etc", "P1 ", "S9"])
def test_unknown_or_malformed_labels_are_invalid_and_the_claim_is_flagged(ref):
    out = ans.validate_answer(raw(paragraph(citations=[cite(ref)])), evidence())
    if ref == "P1 ":  # surrounding whitespace is forgiven, nothing else
        assert out.blocks[0]["citations"] == [1]
        return
    assert out.blocks[0]["citations"] == [] and out.blocks[0]["unsupported"] is True
    assert out.stats.invalid_citations == 1 and out.citations == []


def test_chat_context_is_not_evidence_and_cannot_be_cited():
    out = ans.validate_answer(raw(paragraph(citations=[cite("C1")])), evidence())
    assert out.blocks[0]["unsupported"] is True and out.stats.invalid_citations == 1


def test_a_quote_must_be_a_whitespace_normalized_substring():
    ev = evidence()
    ok = ans.validate_answer(
        raw(
            paragraph(
                citations=[cite("P1", "warehouse processed   twelve thousand orders in March.")]
            )
        ),
        ev,
    )
    assert ok.citations[0]["quotes"] == ["warehouse processed twelve thousand orders in March."]
    bad = ans.validate_answer(
        raw(paragraph(citations=[cite("P1", "the warehouse processed one million orders")])), ev
    )
    assert (
        bad.citations == []
        and bad.blocks[0]["unsupported"] is True
        and bad.stats.invalid_citations == 1
    )


def test_a_fabricated_quote_removes_only_that_citation():
    out = ans.validate_answer(
        raw(
            paragraph(citations=[cite("P1", "never written"), cite("P1", "twelve thousand orders")])
        ),
        evidence(),
    )
    assert out.blocks[0]["citations"] == [1] and out.citations[0]["quotes"] == [
        "twelve thousand orders"
    ]
    assert not out.blocks[0]["unsupported"]


@pytest.mark.parametrize("ref", ["P2", "P3", "S1"])
def test_quotes_are_never_kept_on_ocr_vision_figure_or_section_passages(ref):
    out = ans.validate_answer(raw(paragraph(citations=[cite(ref, "anything at all")])), evidence())
    assert out.blocks[0]["citations"] == [1] and out.citations[0]["quotes"] == []
    assert out.stats.dropped_quotes == 1
    assert out.citations[0]["machine_generated"] is (ref in {"P2", "P3"})


def test_empty_or_blank_quotes_are_ignored():
    out = ans.validate_answer(
        raw(paragraph(citations=[cite("P1", ""), cite("P1", "   ")])), evidence()
    )
    assert out.blocks[0]["citations"] == [1] and out.citations[0]["quotes"] == []


# ---- claims and the repair threshold ---------------------------------------------------------------------------------


def test_a_claim_without_any_citation_is_unsupported_and_counted():
    out = ans.validate_answer(raw(paragraph("a", [cite("P1")]), paragraph("b", [])), evidence())
    assert [b["unsupported"] for b in out.blocks] == [False, True]
    assert (out.stats.claims, out.stats.unsupported) == (
        2,
        1,
    ) and out.stats.unsupported_share == 0.5


def test_bullets_are_claims_one_by_one():
    block = {
        "type": "bullet_list",
        "text": None,
        "title": None,
        "mermaid": None,
        "citations": [],
        "items": [
            {"text": "a", "citations": [cite("P1")]},
            {"text": "b", "citations": [cite("Z9")]},
        ],
    }
    out = ans.validate_answer(raw(block), evidence())
    assert [i["unsupported"] for i in out.blocks[0]["items"]] == [False, True]
    assert out.stats.claims == 2


def test_repair_threshold_is_more_than_thirty_percent_or_nothing_supported():
    def stats(claims, unsupported):
        return ans.ClaimStats(claims=claims, unsupported=unsupported)

    assert not ans.needs_repair(stats(10, 3))  # exactly 30 % is tolerated
    assert ans.needs_repair(stats(10, 4))
    assert ans.needs_repair(stats(1, 1))  # a single unsupported claim is 100 %
    assert not ans.needs_repair(stats(0, 0))
    assert ans.needs_repair(stats(3, 3))


def test_evidence_gaps_are_kept_and_cleaned():
    out = ans.validate_answer(
        raw(paragraph(citations=[cite("P1")]), gaps=["No budget figure", "  ", "\x00bad"]),
        evidence(),
    )
    assert out.evidence_gaps == ["No budget figure", "bad"]


# ---- shape -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"blocks": "x", "evidence_gaps": []},
        {"blocks": [], "evidence_gaps": []},
        {"blocks": [1], "evidence_gaps": []},
        {"blocks": [{"type": "script", "text": "x"}], "evidence_gaps": []},
        {"blocks": [paragraph("")], "evidence_gaps": []},
        {"blocks": [paragraph("x" * 4001)], "evidence_gaps": []},
        {"blocks": [{"type": "bullet_list", "items": [], "citations": []}], "evidence_gaps": []},
        {"blocks": [{"type": "bullet_list", "items": ["x"], "citations": []}], "evidence_gaps": []},
        {
            "blocks": [{"type": "diagram", "mermaid": "", "title": "t", "citations": []}],
            "evidence_gaps": [],
        },
        {"blocks": [paragraph("x")] * 61, "evidence_gaps": []},
        {"blocks": [paragraph("x")], "evidence_gaps": "none"},
        {"blocks": [paragraph("x", "not a list")], "evidence_gaps": []},
    ],
)
def test_a_reply_of_the_wrong_shape_is_refused_whole(bad):
    with pytest.raises(ans.BadAnswer):
        ans.validate_answer(bad, evidence())


def test_control_characters_are_stripped_from_text():
    out = ans.validate_answer(raw(paragraph("he\x00llo\x07 world", [cite("P1")])), evidence())
    assert out.blocks[0]["text"] == "hello world"


def test_markup_in_text_stays_text():
    out = ans.validate_answer(
        raw(paragraph("<img src=x onerror=alert(1)> [x](javascript:alert(1))", [cite("P1")])),
        evidence(),
    )
    assert (
        "<img" in out.blocks[0]["text"]
    )  # kept verbatim: the UI renders text as text, never as HTML


def test_diagram_blocks_are_collected_for_the_parse_check():
    block = {
        "type": "diagram",
        "title": "Flow",
        "mermaid": "flowchart TD\n A-->B",
        "items": None,
        "text": None,
        "citations": [cite("P1")],
    }
    out = ans.validate_answer(raw(paragraph("x", [cite("P1")]), block), evidence())
    assert out.diagrams == [1] and out.blocks[1]["render_status"] == "unchecked"


# ---- the plain-text message --------------------------------------------------------------------------------------------


def test_the_chat_body_is_plain_text_with_citation_marks_and_stays_under_the_limit():
    out = ans.validate_answer(
        raw(
            paragraph("Orders grew.", [cite("P1")]),
            paragraph("Unsourced claim."),
            gaps=["No 2024 data"],
        ),
        evidence(),
    )
    text = ans.render_plain_text(out)
    assert (
        text
        == "Orders grew. [1]\nUnsourced claim. [no citation]\nNot found in the sources: No 2024 data"
    )


def test_a_long_answer_is_cut_with_a_pointer_to_the_card():
    out = ans.validate_answer(
        raw(*[paragraph("word " * 200, [cite("P1")]) for _ in range(10)]), evidence()
    )
    text = ans.render_plain_text(out, 4000, "Open the answer card for the rest and the citations.")
    assert len(text) <= 4000 and text.endswith(
        "Open the answer card for the rest and the citations."
    )
    assert "…" in text


def test_diagrams_appear_in_the_chat_body_only_as_a_title():
    block = {
        "type": "diagram",
        "title": "Flow",
        "mermaid": "flowchart TD\n A-->B",
        "items": None,
        "text": None,
        "citations": [],
    }
    out = ans.validate_answer(raw(block), evidence())
    assert ans.render_plain_text(out) == "[Diagram: Flow]"


# ---- Mermaid: the shared cases ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_static_mermaid_rules_match_the_typescript_cases(case):
    result = mermaid.check_static(case["source"])
    assert result.ok is case["ok"], (case["name"], result)
    if not case["ok"]:
        assert result.code == case["code"]


def test_size_limits():
    assert mermaid.check_static("flowchart TD\n" + "%% pad\n" * 5000).code == "too_long"
    assert mermaid.check_static("flowchart TD\n" + "%\n" * 600).code == "too_many_lines"


def test_diagram_type_detection():
    assert mermaid.diagram_type("%% c\nerDiagram\n A ||--o{ B : x") == "erDiagram"
    assert mermaid.diagram_type("pie") is None


@pytest.mark.skipif(
    not SCRIPT_BUILT,
    reason="run `npm run build` first: packages/mermaid-check/dist/cli.js is missing",
)
async def test_the_node_parse_check_accepts_good_and_rejects_bad_diagrams():
    cfg = make_settings()
    good = "flowchart TD\n  A[Start] --> B{Ok?}\n  B -->|yes| C[Done]"
    syntax = "flowchart TD\n  A[Start --> \n B("
    bad_rule = 'flowchart TD\n  A --> B\n  click A href "x"'
    results = await mermaid.parse_check(cfg, [good, syntax, bad_rule, "pie title x"])
    assert results[0].ok
    assert (
        not results[1].ok
        and results[1].code == "parse_error"
        and "line" in results[1].message.lower()
    )
    assert not results[2].ok and results[2].code == "interaction_not_allowed"
    assert not results[3].ok and results[3].code == "type_not_allowed"


async def test_without_node_diagrams_are_unchecked_but_static_rules_still_apply():
    cfg = make_settings(ai_node_command="no-such-node-binary")
    results = await mermaid.parse_check(
        cfg, ["flowchart TD\n A-->B", "flowchart TD\n A[<b>x</b>] --> B"]
    )
    assert results[0] is None  # could not be parsed here: published `unchecked`, never ok
    assert results[1] is not None and results[1].code == "html_not_allowed"
