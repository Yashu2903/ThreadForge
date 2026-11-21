"""Evidence-ID recall and the task-quality checks (tests/eval/evidence.py, tests/eval/checks.py).

THE POINT OF THIS FILE: the old "retrieval recall" was the share of required PHRASES found in the top-k passages. Each test below
is a case where that number and the number of relevant evidence found disagree, or where retrieval, answer support and
usefulness must not be read as one measure. Pure functions and the synthetic corpus only: no provider, no database.
"""

from __future__ import annotations

import pytest

from tests import analysis_files as af
from tests.eval import corpus, evidence
from tests.eval.checks import (
    Concept,
    concepts_found,
    draws_order,
    invented_names,
    node_labels,
    order_disclosed,
    placeholder_hits,
    unsupported_figures,
)
from tests.eval.corpus import GOLD
from tests.eval.metrics import METRIC_VERSIONS, recall_at_k, required_phrase_coverage_at_k


def passage(name: str, text: str, **loc) -> dict:
    return {"source_name": name, "text": text, **loc}


def phrase_and_evidence(item_id: str, ranked: list[dict], k: int = 3):
    item = next(g for g in GOLD if g.id == item_id)
    texts = [c["text"] for c in ranked]
    return (
        required_phrase_coverage_at_k(texts, item.needles, k),
        evidence.evidence_recall(item_id, ranked)["recall"][f"@{k}"],
    )


# ---- the two measures disagree, in both directions ------------------------------------------------------------------


def test_the_old_name_and_the_accurate_name_are_the_same_computation():
    texts = ["alpha beta", "gamma"]
    assert (
        recall_at_k(texts, ["beta"], 1) == required_phrase_coverage_at_k(texts, ["beta"], 1) == 1.0
    )
    assert METRIC_VERSIONS == {
        "required_phrase_coverage": "phrase-v1",
        "evidence_recall": "evidence-v1",
    }


def test_a_relevant_paraphrase_counts_as_evidence_even_though_it_has_none_of_the_phrases():
    # the answer to "how many parcels did Berlin handle" is on page 1; this chunk says it in other words
    ranked = [
        passage(
            "depots.pdf",
            "Berlin sorted four thousand two hundred packages last May.",
            page_start=1,
            page_end=1,
        )
    ]
    phrase, ev = phrase_and_evidence("qa-berlin", ranked)
    assert phrase == 0.0  # the phrase "4200 parcels" is not there
    assert ev == {"found": 1, "needed": 1, "value": 1.0}


def test_a_required_phrase_in_irrelevant_evidence_is_not_relevant_evidence():
    ranked = [
        passage(
            "depots.pdf", "A note that mentions 4200 parcels in passing.", page_start=5, page_end=5
        )
    ]
    phrase, ev = phrase_and_evidence("qa-berlin", ranked)
    assert phrase == 1.0  # the phrase is in the top 3
    assert ev["value"] == 0.0  # but it is the wrong page


def test_evidence_from_a_source_the_request_did_not_select_does_not_count():
    # the right page number of the WRONG file
    ranked = [passage("other.pdf", "Berlin handled 4200 parcels.", page_start=1, page_end=1)]
    assert phrase_and_evidence("qa-berlin", ranked)[1]["value"] == 0.0


def test_a_page_range_covers_every_page_in_it():
    ranked = [passage("depots.pdf", "two pages in one chunk", page_start=2, page_end=3)]
    assert evidence.evidence_recall("qa-lisbon-storms", ranked)["recall"]["@1"]["value"] == 1.0


# ---- several relevant passages, several acceptable sets, the rank -----------------------------------------------------------


def test_every_unit_of_a_set_is_needed_and_a_unit_counts_once_however_many_chunks_cover_it():
    both = [
        passage("depots.pdf", "Lisbon", page_start=2, page_end=2),
        passage(
            "depots.pdf", "Lisbon again", page_start=2, page_end=2
        ),  # a second chunk of the same page
        passage("march-scan.pdf", "twelve thousand orders", page_start=1, page_end=1),
    ]
    r = evidence.evidence_recall("qa-two-sources", both)
    assert r["recall"]["@1"] == {"found": 1, "needed": 2, "value": 0.5}
    assert r["recall"]["@3"] == {"found": 2, "needed": 2, "value": 1.0}
    assert r["first_hit_rank"] == 1


def test_the_best_acceptable_set_decides(monkeypatch):
    gold = evidence.EvidenceGold("page", (("a.pdf#page=1", "a.pdf#page=2"), ("a.pdf#page=9",)))
    monkeypatch.setitem(evidence.GOLD_EVIDENCE, "x", gold)
    only_nine = [passage("a.pdf", "t", page_start=9, page_end=9)]
    assert (
        evidence.evidence_recall("x", only_nine)["recall"]["@1"]["value"] == 1.0
    )  # the short set is satisfied
    half = [passage("a.pdf", "t", page_start=1, page_end=1)]
    assert evidence.evidence_recall("x", half)["recall"]["@1"] == {
        "found": 1,
        "needed": 2,
        "value": 0.5,
    }


def test_rank_matters_the_hit_is_reported_at_the_rank_it_first_appears():
    ranked = [
        passage("depots.pdf", "wrong", page_start=4, page_end=4),
        passage("depots.pdf", "wrong again", page_start=6, page_end=6),
        passage("depots.pdf", "Berlin", page_start=1, page_end=1),
    ]
    r = evidence.evidence_recall("qa-berlin", ranked)
    assert [r["recall"][f"@{k}"]["value"] for k in (1, 3)] == [0.0, 1.0]
    assert r["first_hit_rank"] == 3 and r["recall"]["@all"]["value"] == 1.0


def test_a_section_is_covered_by_a_chunk_at_or_below_its_path():
    below = [passage("release.md", "t", heading_path=["Release process", "Deploy"])]
    assert evidence.evidence_recall("qa-md-restart", below)["recall"]["@1"]["value"] == 1.0
    sibling = [passage("release.md", "t", heading_path=["Release process", "Build"])]
    assert evidence.evidence_recall("qa-md-restart", sibling)["recall"]["@1"]["value"] == 0.0
    # a parent heading chunk is NOT evidence for a child section
    parent = [passage("release.md", "t", heading_path=["Release process"])]
    assert evidence.evidence_recall("qa-md-restart", parent)["recall"]["@1"]["value"] == 0.0


def test_a_figure_is_a_unit_of_its_own():
    fig = [
        passage(
            "handbook.docx",
            "Figure 1",
            figure_number=1,
            heading_path=["Section 2 overview", "Details"],
        )
    ]
    assert evidence.evidence_recall("figure-handbook", fig)["recall"]["@1"]["value"] == 1.0
    text_only = [passage("handbook.docx", "Section 2", heading_path=["Section 2 overview"])]
    assert evidence.evidence_recall("figure-handbook", text_only)["recall"]["@1"]["value"] == 0.0


# ---- no gold, and routes that do not rank ----------------------------------------------------------------------------------


def test_an_item_without_gold_evidence_has_no_recall_it_is_not_zero():
    r = evidence.evidence_recall(
        "compare-depots-release", [passage("depots.pdf", "x", page_start=1, page_end=1)]
    )
    assert r["gold"] is None and set(r["recall"].values()) == {None}
    assert evidence.evidence_recall("not-a-real-item", [])["gold"] is None


def test_summaries_report_coverage_of_what_was_read_never_recall():
    read = [
        passage("release.md", "t", heading_path=["Release process"]),
        passage("release.md", "t", heading_path=["Release process", "Build"]),
    ]
    cov = evidence.evidence_coverage("summarize-release", read, evidence.FULL_SECTIONS)
    assert cov["strategy"] == "full_sections" and "recall" not in cov
    assert cov["coverage"] == {
        "found": 2,
        "needed": 3,
        "value": pytest.approx(2 / 3),
    }  # Deploy was not read


def test_the_aggregate_keeps_recall_and_coverage_apart_and_counts_items_without_gold():
    records = [
        {
            "evidence": evidence.evidence_recall(
                "qa-berlin", [passage("depots.pdf", "x", page_start=1, page_end=1)]
            )
        },
        {
            "evidence": evidence.evidence_recall(
                "qa-lisbon-storms", [passage("depots.pdf", "x", page_start=4, page_end=4)]
            )
        },
        {"evidence": evidence.evidence_recall("compare-depots-release", [])},
        {
            "evidence": evidence.evidence_coverage(
                "summarize-release",
                [passage("release.md", "t", heading_path=["Release process", "Build"])],
                evidence.FULL_SECTIONS,
            )
        },
    ]
    s = evidence.summarize_evidence(records)
    assert s["ranked_items"] == 3 and s["ranked_items_without_gold"] == 1 and s["read_items"] == 1
    at1 = s["recall"]["@1"]
    assert (
        at1["items"],
        at1["mean"],
        at1["found_units"],
        at1["needed_units"],
        at1["items_all_found"],
    ) == (2, 0.5, 1, 2, 1)
    assert s["coverage"]["items"] == 1 and (
        s["coverage"]["found_units"],
        s["coverage"]["needed_units"],
    ) == (2, 3)  # Deploy unread


# ---- the answer is a separate question from the retrieval ---------------------------------------------------------------------


def test_the_right_source_retrieved_but_the_answer_uses_it_wrongly_retrieval_and_support_disagree():
    ranked = [
        passage(
            "depots.pdf", "The Berlin depot handled 4200 parcels in May.", page_start=1, page_end=1
        )
    ]
    retrieval = evidence.evidence_recall("qa-berlin", ranked)["recall"]["@1"]["value"]
    good, bad = (
        "The Berlin depot handled 4,200 parcels in May.",
        "The Berlin depot handled 5,200 parcels in May.",
    )
    material = [c["text"] for c in ranked]
    assert retrieval == 1.0  # retrieval succeeded for both answers
    assert unsupported_figures(good, material) == []  # 4,200 is 4200
    assert unsupported_figures(bad, material) == ["5200 parcel"]  # but the second answer invented a figure
    assert concepts_found(
        bad, [Concept("berlin", (r"berlin",)), Concept("figure", (r"\b4,?200\b",))]
    )["missing"] == ["figure"]


def test_a_derived_summary_that_keeps_the_meaning_and_one_that_distorts_it():
    from app.ai.edit import qualifiers

    original = "The Build gate compiles the sources. Alice owns Build and Bob owns Deploy. There are three gates."
    kept = "Three gates exist; Alice owns Build, Bob owns Deploy, and Build compiles the sources."
    distorted = "Five gates exist; Bob owns Build and Alice owns Deploy."
    rubric = [
        Concept("alice-build", (r"alice owns build",)),
        Concept("bob-deploy", (r"bob owns deploy",)),
        Concept("three", (r"\bthree\b",)),
    ]
    assert qualifiers.compare(original, kept) == []
    assert concepts_found(kept, rubric)["missing"] == []
    assert [f.code for f in qualifiers.compare(original, distorted)] == [
        "figure_changed"
    ]  # three -> five
    assert concepts_found(distorted, rubric)["missing"] == [
        "alice-build",
        "bob-deploy",
        "three",
    ]  # swapped owners are not "Alice owns Build"


# ---- the rubric checks themselves ------------------------------------------------------------------------------------------


def test_concepts_accept_any_listed_surface_form_and_report_what_is_missing():
    rubric = [Concept("date", (r"30 june", r"june 30")), Concept("budget", (r"120,?000",))]
    got = concepts_found("Atlas moves by June 30.", rubric)
    assert (
        got["found"] == ["date"]
        and got["missing"] == ["budget"]
        and got["share"] == 0.5
        and got["of"] == 2
    )
    assert concepts_found("anything", [])["share"] is None


def test_the_placeholder_diagnostic_catches_the_p7_sentence_and_does_not_clear_an_empty_summary():
    p7 = "In summary, the preceding discussion brings together the key points presented above."
    assert placeholder_hits(p7)
    assert not placeholder_hits(
        "Atlas moves the order system to the cloud by 30 June at a cost of 120,000 euros."
    )
    # absence of the placeholder is not quality: this says nothing and is not flagged, which is why concepts decide
    assert not placeholder_hits("It is what it is.")


def test_order_disclosure_reads_what_the_model_said_not_whether_it_is_true():
    assert order_disclosed(["Build is drawn before Deploy."], [])
    assert order_disclosed([], ["The sources do not state the order of the steps."])
    assert not order_disclosed([], ["The sources name two of the three gates."])
    assert not order_disclosed([], [])


def test_diagram_helpers_find_ordering_arrows_labels_and_invented_stages():
    flow = 'flowchart TD\n  A["Build — Alice"] --> B["Deploy — Bob"]\n  R[Release] -.-> C[Gate three: Verify]'
    assert draws_order(flow) and not draws_order(
        "flowchart LR\n  subgraph S\n    A[Build]\n    B[Deploy]\n  end"
    )
    assert node_labels(flow) == ["Build — Alice", "Deploy — Bob", "Release", "Gate three: Verify"]
    material = "Every release goes through three gates. Build compile the sources. Deploy restarts the workers. Alice Bob"
    assert invented_names(flow, material) == [
        "Gate three: Verify"
    ]  # 'Verify' is a stage the source never names
    assert invented_names(flow, material, allowed=["Gate three: Verify"]) == []


# ---- the gold evidence is derived from the fixtures, never from a model's output ----------------------------------------------


def page_of_depots(needle: str) -> int:
    for i, (city, parcels, note) in enumerate(corpus.DEPOTS, start=1):
        if needle in f"{city} {parcels} parcels {note}":
            return i
    raise AssertionError(needle)


def test_every_gold_page_is_the_page_that_really_holds_the_needle_in_the_fixture():
    assert evidence.GOLD_EVIDENCE["qa-berlin"].sets == (
        (f"depots.pdf#page={page_of_depots('4200 parcels')}",),
    )
    assert evidence.GOLD_EVIDENCE["qa-lisbon-storms"].sets == (
        (f"depots.pdf#page={page_of_depots('closed early twice because of storms')}",),
    )
    first_scan = " ".join(corpus.SCAN_PAGES[0])
    assert "twelve thousand" in first_scan and "shortage of drivers" in first_scan
    assert (
        evidence.GOLD_EVIDENCE["qa-scan-orders"].sets
        == evidence.GOLD_EVIDENCE["qa-scan-delays"].sets
        == (("march-scan.pdf#page=1",),)
    )
    two = evidence.GOLD_EVIDENCE["qa-two-sources"].sets[0]
    assert two == (f"depots.pdf#page={page_of_depots('3100 parcels')}", "march-scan.pdf#page=1")


def test_the_markdown_gold_sections_hold_the_needles_and_the_document_gold_is_the_risk_block():
    md = af.MARKDOWN_SAMPLE
    deploy = md.split("## Deploy")[1]
    assert "Bob" in deploy and "restarts the workers" in deploy
    assert "compile the sources" in md.split("## Build")[1].split("## Deploy")[0]
    assert (
        evidence.GOLD_EVIDENCE["qa-md-owner"].sets
        == evidence.GOLD_EVIDENCE["qa-md-restart"].sets
        == ((evidence.DEPLOY,),)
    )
    assert corpus.DOCUMENT_ITEMS[4][2] == "Vendor delays could slip the schedule."  # block index 4
    assert evidence.GOLD_EVIDENCE["review-launch-plan"].sets == (("launch-plan#block=4",),)


def test_every_gold_item_has_a_declared_strategy_and_gold_matches_the_acceptance_dataset():
    ids = {g.id for g in GOLD}
    assert (
        set(evidence.GOLD_EVIDENCE) == ids
    )  # one entry per acceptance item, none invented, none missing
    for g in GOLD:
        assert evidence.STRATEGY_OF_TASK[g.task] in (
            evidence.RANKED,
            evidence.FULL_SECTIONS,
            evidence.FULL_DOCUMENT,
        )


def test_fixture_identity_is_stable_for_every_fixture_even_those_whose_bytes_are_not():
    a, b = evidence.corpus_identity(), evidence.corpus_identity()
    assert a == b and set(a) == {"depots.pdf", "march-scan.pdf", "handbook.docx", "release.md"}
    assert (
        a["march-scan.pdf"]["kind"] == "content:page_texts"
        and a["handbook.docx"]["kind"] == "content:document_xml+media"
    )
    assert all(len(v["sha256"]) == 64 for v in a.values())


def test_the_gold_fingerprint_changes_when_the_gold_changes(monkeypatch):
    before = evidence.gold_fingerprint()
    assert before == evidence.gold_fingerprint()
    monkeypatch.setitem(
        evidence.GOLD_EVIDENCE,
        "qa-berlin",
        evidence.EvidenceGold("page", (("depots.pdf#page=2",),)),
    )
    assert evidence.gold_fingerprint() != before


def test_the_policies_are_written_down_for_the_manifest():
    p = evidence.policies()
    assert {
        "unit",
        "acceptable_sets",
        "multiple_passages",
        "recall_at_k",
        "aggregate",
        "missing_gold",
        "unranked_routes",
        "fixture_identity",
    } <= set(p)
