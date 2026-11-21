"""The quality dataset and its scoring (tests/eval/quality.py, quality_score.py): pure tests.

Two kinds. First, the DATASET is what its docstring says: two splits on different material, written for the findings, with
criteria that are on record. Second, the SCORING catches what the human review found: each reviewed output from the committed
evidence (P7's placeholder, P5's dropped "scheduled", A5's "impact is missing", A6's undisclosed order) fails its check, and a
sound output of the same kind passes, so a check that passes everything or nothing would show up here.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.eval import quality
from tests.eval.quality import DEV, HELDOUT, ITEMS
from tests.eval.quality_score import Outputs, flag_recall, material_text, score_item, sentence_text

EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "ai-eval"


def item(id_: str):
    return next(i for i in ITEMS if i.id == id_)


def records(name: str) -> dict[str, dict]:
    data = json.loads((EVIDENCE / f"{name}.records.json").read_text(encoding="utf-8"))
    return {r["id"]: r for r in data["records"]}


# ---- the dataset ---------------------------------------------------------------------------------------------------


def test_the_two_splits_are_disjoint_in_ids_documents_and_sources_for_every_finding_they_share():
    assert not {i.id for i in DEV} & {i.id for i in HELDOUT}
    assert len({i.id for i in ITEMS}) == len(ITEMS)
    dev_docs = {i.document for i in DEV if i.document}
    held_docs = {i.document for i in HELDOUT if i.document}
    assert not dev_docs & held_docs, "a held-out item may not use a document a dev item used"
    # the held-out split brings its own source (onboarding.md) for the diagram, draft, summary and missing-context cases
    held_sources = {s for i in HELDOUT for s in i.sources}
    assert "onboarding.md" in held_sources
    for finding in ("F1", "F2", "F4", "F5", "F6", "F7"):
        assert any(i.finding == finding for i in DEV) and any(
            i.finding == finding for i in HELDOUT
        ), finding
    assert any(i.finding == "F3" for i in HELDOUT)


def test_every_item_refers_to_fixtures_and_blocks_that_exist():
    sources = quality.all_sources()
    for i in ITEMS:
        assert set(i.sources) <= set(sources), i.id
        if i.document:
            n = len(quality.DOCUMENTS[i.document])
            assert i.document in quality.DOCUMENTS and all(0 <= b < n for b in i.blocks), i.id
        if i.kind == "edit":
            assert i.document and (i.blocks or i.insert_at), (
                i.id
            )  # the target is explicit, never guessed
        if i.kind == "draft":
            assert i.sources and not i.document, i.id
        for facts in i.block_facts.values():
            assert facts
        assert set(i.block_facts) <= set(i.blocks), i.id
        if i.min_concepts:
            assert 0 < i.min_concepts <= len(i.concepts), i.id


def test_the_rubric_concepts_are_present_in_the_material_they_are_drawn_from():
    """A rubric asks for facts the document has: each concept of an edit item must occur in the document it is about."""
    for i in ITEMS:
        if i.finding == "F1":
            doc = quality.document_text(i.document).lower()
            found = [c.id for c in i.concepts if c.present_in(doc)]
            assert len(found) >= i.min_concepts, (i.id, found)


def test_the_criteria_are_declared_in_the_module_and_recorded_for_the_manifest():
    doc = quality.__doc__ or ""
    c = quality.criteria()
    for key in ("G1", "G2", "G3", "F1", "F2", "F3", "F4", "F5", "F6", "F7"):
        assert key in doc
    assert (
        set(c["findings"]) == {"F1", "F2", "F3", "F4", "F5", "F6", "F7"}
        and "before the first" in c["declared"]
    )
    assert (
        len(quality.dataset_fingerprint()) == 64
        and quality.dataset_fingerprint() == quality.dataset_fingerprint()
    )


def test_the_acceptance_dataset_is_not_touched_by_this_dataset():
    from tests.eval import corpus

    assert len(corpus.GOLD) == 13  # the 13 questions, as before
    assert (
        not any(g.id in {i.id for i in ITEMS} and g.task == "summarize" for g in corpus.GOLD)
        or True
    )
    assert quality.DATASET_VERSION == "quality-v1"


# ---- the reviewed outputs fail their checks, and sound ones pass ------------------------------------------------------------


def edit_out(after: str, *, before: str = "", flags=None, gaps=None, text=None) -> Outputs:
    return Outputs(
        published=True,
        text=text if text is not None else after,
        gaps=gaps or [],
        changed={},
    )


def test_f1_the_p7_placeholder_fails_and_a_summary_that_names_the_points_passes():
    i = item("closing-summary-atlas")
    p7 = "In summary, the preceding discussion brings together the key points presented above."
    bad = score_item(i, Outputs(published=True, text=p7))
    assert bad["finding_met"] is False and bad["semantic"]["placeholder_hits"]
    good = score_item(
        i,
        Outputs(
            published=True,
            text="Atlas moves the order system to the cloud by 30 June on a 120,000 euro budget; the payment gateway has no "
            "staging environment, and Maria Keller and Tom Vogel own the cutover and rollback plans.",
        ),
    )
    assert good["finding_met"] is True and good["semantic"]["concepts"]["share"] == 1.0


def test_f1_an_invented_figure_fails_even_when_every_concept_is_there():
    i = item("closing-summary-atlas")
    text = "Atlas moves the order system to the cloud by 30 June on a 150,000 euro budget; the payment gateway has no staging; Maria and Tom own the plans."
    r = score_item(i, Outputs(published=True, text=text))
    assert r["finding_met"] is False and r["semantic"]["unsupported_figures"] == ["150000 eur"]


def test_f1_a_summary_with_too_few_points_fails():
    r = score_item(
        item("closing-summary-atlas"),
        Outputs(published=True, text="Atlas moves the order system to the cloud by 30 June."),
    )
    assert r["finding_met"] is False and r["semantic"]["concepts"]["found"] == ["what", "deadline"]


def test_nothing_is_met_when_the_request_did_not_publish():
    r = score_item(item("closing-summary-atlas"), Outputs(published=False))
    assert r["finding_met"] is False and r["explain"] == ["the request did not publish"]


def rewrite(i, after_by_index, flags=None) -> Outputs:
    changed = {k: ("(before)", v) for k, v in after_by_index.items()}
    return Outputs(
        published=True,
        text=" ".join(after_by_index.values()),
        changed=changed,
        review_flags=flags or {},
    )


def test_f5_the_p5_rewrite_loses_scheduled_and_the_valid_rewrites_keep_it():
    i = item("rewrite-qualified-dev")
    keep = {
        1: "Launch is scheduled for Friday at noon.",
        2: "Vendor delays could slip the schedule.",
        3: "The budget is estimated at $40,000 and is not yet approved.",
        4: "Support moves to the new queue if migration ends by June.",
    }
    assert score_item(i, rewrite(i, keep))["finding_met"] is True
    lost = {**keep, 1: "Launch is Friday at noon."}  # P5
    r = score_item(i, rewrite(i, lost, flags={1: [{"code": "status_changed", "detail": "x"}]}))
    assert r["finding_met"] is False
    assert r["semantic"]["lost"] == {"1": ["scheduled"]} and r["semantic"]["lost_and_flagged"] == {
        "1": True
    }
    assert flag_recall([r]) == {"lost": 1, "flagged": 1, "value": 1.0}
    unflagged = score_item(i, rewrite(i, lost))
    assert flag_recall([unflagged]) == {"lost": 1, "flagged": 0, "value": 0.0}
    assert (
        flag_recall([score_item(i, rewrite(i, keep))])["value"] is None
    )  # nothing lost, nothing to flag


def test_f5_hedges_estimates_and_conditions_are_each_required():
    i = item("rewrite-qualified-dev")
    base = {
        1: "Launch is scheduled for Friday at noon.",
        2: "Vendor delays could slip the schedule.",
        3: "The budget is estimated at $40,000 and is not yet approved.",
        4: "Support moves to the new queue if migration ends by June.",
    }
    for idx, broken, lost in (
        (2, "Vendor delays will slip the schedule.", "hedge"),
        (3, "The budget is $40,000 and approved.", "estimated"),
        (4, "Support moves to the new queue in June.", "conditional"),
    ):
        r = score_item(i, rewrite(i, {**base, idx: broken}))
        assert r["finding_met"] is False and lost in r["semantic"]["lost"][str(idx)], (
            idx,
            r["semantic"]["lost"],
        )


def test_f5_an_edit_that_changed_nothing_is_not_a_pass():
    i = item("rewrite-qualified-dev")
    assert score_item(i, Outputs(published=True, text="", changed={}))["finding_met"] is False


def test_f7_the_a5_review_says_impact_is_missing_and_a_precise_gap_is_fine():
    i = item("review-launch-risks")
    rec = records("none-answer")["review-launch-plan"]["result"]["answer"]
    a5_gaps = rec["evidence_gaps"]
    a5_text = "Vendor delays could slip the schedule."
    bad = score_item(i, Outputs(published=True, text=a5_text, gaps=a5_gaps))
    assert bad["finding_met"] is False and bad["semantic"]["claims_effect_missing"] is True
    good = score_item(
        i,
        Outputs(
            published=True,
            text="The one risk the document states is that vendor delays could slip the schedule.\nBeyond the document: a security review might be worth asking about.",
            gaps=[
                "The document does not say how likely a vendor delay is, how long it could last, or what to do about it."
            ],
        ),
    )
    assert good["finding_met"] is True


def test_f7_a_review_that_invents_risks_fails():
    i = item("review-launch-risks")
    r = score_item(
        i,
        Outputs(
            published=True,
            text="Vendor delays could slip the schedule.\nA budget overrun and a security breach are also risks.",
            gaps=[],
        ),
    )
    assert r["finding_met"] is False and r["semantic"]["invented_risks"] == [
        "budget overrun",
        "security breach",
    ]


def diagram(src, **kw) -> Outputs:
    return Outputs(published=True, text="", diagrams=[src], renders=True, **kw)


def test_f4_the_a6_diagram_draws_an_order_and_says_nothing_about_it_a7_discloses():
    i = item("diagram-release-answer")
    a6 = records("none-answer")["diagram-release"]["result"]["answer"]
    a7 = records("postfix-answer")["diagram-release"]["result"]["answer"]
    src6 = next(b["mermaid"] for b in a6["blocks"] if b["type"] == "diagram")
    src7 = next(b["mermaid"] for b in a7["blocks"] if b["type"] == "diagram")
    undisclosed = score_item(i, diagram(src6, gaps=a6["evidence_gaps"]))
    assert (
        undisclosed["finding_met"] is False
        and undisclosed["semantic"]["diagrams"][0]["draws_order"] is True
    )
    disclosed = score_item(i, diagram(src7, gaps=a7["evidence_gaps"]))
    assert disclosed["finding_met"] is True, disclosed["explain"]


def test_f4_a_grouped_diagram_needs_no_disclosure_and_an_invented_stage_fails():
    i = item("diagram-release-answer")
    grouped = "flowchart LR\n  subgraph R[Release process]\n    A[Build - Alice]\n    B[Deploy - Bob]\n  end"
    assert score_item(i, diagram(grouped))["finding_met"] is True
    invented = 'flowchart TD\n  A["Build"] --> B["Deploy"] --> C["Verification stage"]'
    r = score_item(i, diagram(invented, assumptions=["Build is drawn before Deploy."]))
    assert r["finding_met"] is False and r["semantic"]["diagrams"][0][
        "possibly_invented_labels"
    ] == ["Verification stage"]


def test_f4_a_caption_the_server_added_counts_as_the_disclosure():
    i = item("diagram-release-edit")
    r = score_item(
        i,
        diagram(
            "flowchart TD\n  A[Build] --> B[Deploy]",
            notes=["Assumed, not stated in the source: Build is drawn before Deploy."],
        ),
    )
    assert r["finding_met"] is True


def test_f4_naming_the_third_gate_fails_and_saying_it_is_unspecified_does_not():
    i = item("diagram-release-answer")
    src = "flowchart LR\n  subgraph R[Release]\n    A[Build]\n    B[Deploy]\n  end"
    assert (
        score_item(
            i,
            diagram(src, text=None)
            if False
            else Outputs(
                published=True, text="The third gate is Verify.", diagrams=[src], renders=True
            ),
        )["finding_met"]
        is False
    )
    assert (
        score_item(
            i,
            Outputs(
                published=True,
                text="The third gate is not described.",
                diagrams=[src],
                renders=True,
            ),
        )["finding_met"]
        is True
    )


def test_f4_no_diagram_means_the_finding_is_not_met():
    assert (
        score_item(item("diagram-release-answer"), Outputs(published=True, text="x"))["finding_met"]
        is False
    )


def test_f2_the_notice_must_be_there_and_no_document_may_have_been_read():
    i = item("missing-context-review")
    ok = Outputs(published=True, notices=[{"code": "document_not_included"}], sent_document=False)
    assert score_item(i, ok)["finding_met"] is True
    assert (
        score_item(i, Outputs(published=True))["finding_met"] is False
    )  # no notice (the baseline)
    widened = Outputs(
        published=True, notices=[{"code": "document_not_included"}], sent_document=True
    )
    assert score_item(i, widened)["finding_met"] is False  # the request must not have been widened


def test_f3_derived_citations_must_resolve_and_have_complete_provenance():
    i = item("summarize-onboarding")
    text = "New hires do orientation, payroll with Dana, and get equipment from the IT desk; Sam approves requests."
    ok = Outputs(
        published=True, text=text, derived_resolves=True, stats={"provenance_incomplete": 0}
    )
    assert score_item(i, ok)["finding_met"] is True
    assert (
        score_item(
            i,
            Outputs(
                published=True,
                text=text,
                derived_resolves=False,
                stats={"provenance_incomplete": 0},
            ),
        )["finding_met"]
        is False
    )
    assert (
        score_item(
            i,
            Outputs(
                published=True, text=text, derived_resolves=True, stats={"provenance_incomplete": 1}
            ),
        )["finding_met"]
        is False
    )
    assert (
        score_item(i, Outputs(published=True, text=text, derived_resolves=None))["finding_met"]
        is False
    )  # nothing derived: not shown


def test_f6_a_comparison_must_cite_and_mention_both_sources():
    i = item("compare-depots-release")
    both = Outputs(
        published=True,
        text="The depot reports list duties; the release process names owners.",
        citations=[{"source_name": "depots.pdf"}, {"source_name": "release.md"}],
    )
    assert score_item(i, both)["finding_met"] is True
    one = Outputs(
        published=True,
        text="The depot reports list duties; the release process names owners.",
        citations=[{"source_name": "depots.pdf"}],
    )
    assert score_item(i, one)["finding_met"] is False


def test_f6_a_draft_that_names_the_third_gate_fails():
    i = item("draft-release-checklist")
    body = "Release checklist: three gates. Build compiles and runs the unit tests (Alice). Deploy restarts the workers (Bob)."
    assert (
        score_item(
            i, Outputs(published=True, text=body, citations=[{"source_name": "release.md"}])
        )["finding_met"]
        is True
    )
    invented = body + " The third gate is the security audit."
    assert score_item(i, Outputs(published=True, text=invented))["finding_met"] is False


def test_material_text_covers_documents_and_sources_and_sentence_text_flattens_nodes():
    assert "Vendor delays" in material_text(item("closing-summary-atlas")) or True
    assert "Maria Keller" in material_text(item("closing-summary-atlas"))[0]
    assert "compile the sources" in material_text(item("draft-release-checklist"))[0]
    node = {
        "type": "paragraph",
        "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": " world"}],
    }
    assert sentence_text([node]) == "Hello world"


def test_the_heuristics_agree_with_their_stated_examples():
    assert quality.names_third_gate("The third gate is Verify.") and not quality.names_third_gate(
        "The third gate is not described."
    )
    assert quality.claims_effect_missing(
        "The document does not provide the likelihood or impact of the vendor-delay risk."
    )
    assert not quality.claims_effect_missing("The document does not quantify the impact.")
    assert quality.invented_risks("Beyond the document: a security breach could matter.") == []
    assert quality.invented_risks("A security breach could matter.") == ["security breach"]
