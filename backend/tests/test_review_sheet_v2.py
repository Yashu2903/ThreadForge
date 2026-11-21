"""The review material v2 (tests/eval/build_review_sheet_v2.py): built from small synthetic records, in a temporary folder.

What must hold: every item is a file of its own with the complete material in the stated order; a model-written summary is labelled as
derived and never presented as a quotation; the original text a citation opens to is shown in full; an earlier run's missing evidence is
marked unavailable rather than reconstructed; automated results carry their definitions; the agent assessment is labelled as such and the
human fields are empty; nothing local or secret is in the output; the architecture figure and its warning about arrowheads are included.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.eval import build_review_sheet_v2 as b

LONG = "Sentence of the original text that is long enough to be cut by a table cell. " * 12


def answer_record(rid="summarize-release") -> dict:
    return {
        "id": rid,
        "published": True,
        "outcome": "published",
        "request_id": "11111111-1111-1111-1111-111111111111",
        "latency_s": 7.7,
        "ledger": {"failed_calls": 0},
        "routing": {
            "model_route": "summarize",
            "final_route": "summarize",
            "fallback_used": False,
            "hint": "auto",
        },
        "required_phrase_coverage": {"@1": None},
        "evidence": {
            "strategy": "full_sections",
            "coverage": {"found": 3, "needed": 3, "value": 1.0},
        },
        "citation_clean": True,
        "cites_expected_sources": True,
        "derived_citations_resolve": True,
        "provenance_incomplete": 0,
        "candidates": [
            {
                "rank": 1,
                "label": "S1",
                "source_name": "release.md",
                "heading_path": ["Release process"],
                "text": None,
            },
        ],
        "citation_origins": {
            "1": {
                "origin": [
                    {
                        "kind": "chunk",
                        "source_name": "release.md",
                        "version_no": 1,
                        "heading_path": ["Release process", "Build"],
                        "ordinal": 1,
                        "extraction": "plain",
                        "integrity": "verified",
                        "text": LONG,
                    }
                ]
            }
        },
        "result": {
            "answer": {
                "blocks": [
                    {
                        "type": "paragraph",
                        "text": "Every release passes three gates.",
                        "citations": [1],
                        "unsupported": False,
                    }
                ],
                "evidence_gaps": ["The third gate is not described."],
            },
            "citations": [
                {
                    "n": 1,
                    "label": "S1",
                    "kind": "section",
                    "source_name": "release.md",
                    "locator": {"heading_path": ["Release process", "Build"]},
                    "extraction": "plain",
                    "derived": True,
                    "derivation": {"kind": "section_summary", "method": "map-v1"},
                    "provenance": "complete",
                    "excerpt": "The Build gate compiles the sources.",
                    "quotes": [],
                    "machine_generated": False,
                }
            ],
            "coverage": {
                "sources": [
                    {
                        "source_name": "release.md",
                        "version_no": 1,
                        "source_version_id": "v-1",
                        "mode": "full",
                        "sections_read": 3,
                        "sections_total": 3,
                    }
                ],
            },
        },
    }


def historical_proposal_record() -> dict:
    para = {
        "type": "paragraph",
        "attrs": {"id": "x"},
        "content": [
            {
                "type": "text",
                "text": "In summary, the preceding discussion brings together the key points.",
            }
        ],
    }
    return {
        "id": "edit-end-summary",
        "published": True,
        "outcome": "published",
        "first_try_valid": False,
        "repaired": True,
        "problems": ["wrong_operation_count"],
        "resolves": True,
        "citation_valid": None,
        "operations": 1,
        "instruction": "Add a closing summary paragraph.",
        "preview": {
            "kind": "edit",
            "changes": [
                {
                    "op": 0,
                    "kind": "insert_blocks_after",
                    "before": [],
                    "after": [para],
                    "reason": "r",
                    "citations": [],
                }
            ],
        },
    }


@pytest.fixture
def built(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "new-answer.records.json").write_text(
        json.dumps({"records": [answer_record()]}), encoding="utf-8"
    )
    (evidence / "run1-proposal.records.json").write_text(
        json.dumps({"records": [historical_proposal_record()]}), encoding="utf-8"
    )
    monkeypatch.setattr(b, "EVIDENCE", evidence)
    monkeypatch.setattr(b, "OUT", evidence / "review")
    monkeypatch.setattr(b, "SAMPLE", [s for s in b.SAMPLE if s[0] in ("A3", "P7h")])
    monkeypatch.setattr(
        b, "AGENT", {"A3": ["Each statement traces to a section; check the originals.", "Short."]}
    )
    assert b.main() == 0
    return evidence / "review"


def read(built: Path, name: str) -> str:
    return (built / "items" / f"{name}.md").read_text(encoding="utf-8")


def test_every_item_is_a_file_of_its_own_and_the_index_links_to_it(built):
    assert sorted(p.name for p in (built / "items").iterdir()) == ["A3.md", "P7h.md"]
    index = (built / "human-review-sheet.md").read_text(encoding="utf-8")
    assert "[A3](items/A3.md)" in index and "[P7h](items/P7h.md)" in index
    assert "SYNTHETIC" in index and "kept unchanged" in index


def test_the_sections_come_in_the_stated_order(built):
    text = read(built, "A3")
    heads = re.findall(r"^## (.+)$", text, re.M)
    assert [h.split(".")[0] for h in heads[:5]] == ["1", "2", "3", "4", "5"]
    assert heads[-2].startswith("6. Agent assessment") and heads[-1].startswith("7. Human review")


def test_a_derived_summary_is_labelled_and_the_original_text_is_shown_in_full(built):
    text = read(built, "A3")
    assert "DERIVED: a summary written by the model, shown as such, never as a quotation" in text
    assert "**Model-written summary** shown to the reader" in text
    assert "> " + LONG.strip() in text.replace("\n> ", " ") or LONG.strip() in text.replace(
        "\n> ", " "
    )
    assert "stored-text check: **verified**" in text and "release.md` version 1" in text
    assert "extraction: plain" in text and "Release process > Build" in text


def test_the_request_context_and_the_full_output_are_present(built):
    text = read(built, "A3")
    assert (
        "Summarize the release process." in text
    )  # the instruction exactly as in the acceptance dataset
    assert "11111111-1111-1111-1111-111111111111" in text
    assert "`release.md` version 1 (`v-1`), read as **full**: 3 of 3 sections" in text
    assert (
        "Every release passes three gates." in text and "The third gate is not described." in text
    )
    assert "*(cites [1])*" in text


def test_an_earlier_run_that_did_not_record_something_says_unavailable_it_is_not_reconstructed(
    built,
):
    text = read(built, "P7h")
    assert "`not recorded`" in text  # no request id in that run
    assert "HISTORICAL" in text
    assert "unavailable" in text.lower()
    assert (
        "In summary, the preceding discussion brings together the key points." in text
    )  # the output itself is kept
    assert "unavailable in this run" in text  # review flags and evidence were not recorded then


def test_automated_results_carry_their_definitions_and_say_what_they_are_not(built):
    text = read(built, "A3")
    assert "| Check | What it is | Result |" in text
    assert "required_phrase_coverage (phrase-v1)" in text and "NOT retrieval recall" in text
    assert "STRUCTURE only" in text and "says nothing about support" in text
    assert "mechanical; none is a judgement of truth, completeness or usefulness" in text


def test_the_agent_assessment_is_labelled_and_the_human_fields_are_empty(built):
    text = read(built, "A3")
    assert "it is NOT a human review and it can be wrong" in text
    assert "* Each statement traces to a section; check the originals." in text
    human = text.split("## 7. Human review")[1]
    rows = [
        line
        for line in human.splitlines()
        if line.startswith("| ") and "---" not in line and "Field" not in line
    ]
    assert rows and all(
        re.fullmatch(r"\| .+ \| \|", line) for line in rows
    )  # every second cell is empty
    assert "Edit safety" not in human  # an answer has no edit-safety field
    p7 = read(built, "P7h").split("## 7. Human review")[1]
    assert "Edit safety" in p7 and "Meaning preserved" in p7


def test_an_unwritten_assessment_is_visibly_unwritten_not_blank(built):
    assert "not written: the reading is added by hand after the run" in read(built, "P7h")


def test_the_fixtures_include_the_original_figure_and_the_warning_about_arrowheads(built):
    fx = built / "fixtures"
    assert (fx / "architecture-figure.png").read_bytes().startswith(b"\x89PNG")
    for name in (
        "release.md",
        "onboarding.md",
        "depots.pdf",
        "launch-plan.md",
        "march-scan-page-1.png",
    ):
        assert (fx / name).is_file(), name
    readme = (fx / "README.md").read_text(encoding="utf-8")
    assert (
        "no arrowhead" in readme
        and "SYNTHETIC" in readme
        and "never says which comes first" in readme
    )


def test_nothing_local_or_secret_is_in_the_output(built):
    blob = "\n".join(p.read_text(encoding="utf-8") for p in built.rglob("*.md"))
    assert not re.search(
        r"C:\\\\?Users|/Users/|flood|sk-[A-Za-z0-9]{10,}|AIza[0-9A-Za-z_-]{20,}|api[_-]?key",
        blob,
        re.I,
    )


def test_blocks_render_lists_tables_headings_and_diagrams_readably():
    doc = [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Plan"}]},
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                    ],
                }
            ],
        },
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableHeader",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "A"}]}
                            ],
                        }
                    ],
                }
            ],
        },
        {"type": "diagram", "content": [{"type": "text", "text": "flowchart TD\n  A --> B"}]},
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Assumed, not stated: x", "marks": [{"type": "italic"}]}
            ],
        },
    ]
    text = b.render_blocks(doc)
    assert (
        "##### Plan" in text
        and "- one" in text
        and "| A |" in text
        and "```mermaid\nflowchart TD\n  A --> B\n```" in text
    )
    assert "*Assumed, not stated: x*" in text
