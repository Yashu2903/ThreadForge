"""Build the review material v2 (docs/evidence/ai-eval/review-2026-11-20/) from committed run records. It calls no provider.

    cd backend
    PYTHONPATH=. uv run python -m tests.eval.build_review_sheet_v2

The first sheet (build_review_sheet.py -> ../human-review-sheet.md) is kept unchanged as the document that was reviewed. This one
answers that review: every sampled item is a FILE of its own (`items/<id>.md`) holding the complete material, so nothing is cut to fit
a table; `human-review-sheet.md` is the index. An item shows, in this order:

  1 the request       instruction as sent, operation and route, the source versions selected, the open document's captured state,
                      the target of an edit, and how much of the document the model was shown
  2 what it read      the ORIGINAL passages (ranked, full text, locator, extraction method), the sections read, or the document blocks
  3 the output        the complete answer or proposal; for an edit, the complete target before and after
  4 citations         every citation with what the reader sees (a model-written summary is labelled as derived), and what it OPENS
                      TO: the original text, locator, integrity of the stored text, and the original file or figure
  5 automated results each with its definition; mechanical, never a judgement of truth, completeness or usefulness
  6 agent assessment  written by hand after reading the output (AGENT below); it is a reading, labelled as one, not a human review
  7 human review      empty

Where a historical run did not record something (the first runs kept no original passage text), the item says `unavailable` and why.
No text here is edited: outputs are copied from the records. Synthetic fixtures only (fixtures/).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from tests import analysis_files as af
from tests.eval import corpus, quality
from tests.eval.proposals import GOLD as PGOLD

EVIDENCE = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "ai-eval"
OUT = EVIDENCE / "review-2026-11-20"

# logical run -> records file (without .records.json)
RUNS = {
    "answer": "new-answer",
    "proposal": "new-proposal",
    "quality_dev": "new-quality-dev",
    "quality_heldout": "new-quality-heldout",
    "quality_baseline": "baseline-quality",
    "baseline_answer": "none-answer",
    "postfix_answer": "postfix-answer",
    "baseline_proposal": "postfix-proposal",
    "run1_proposal": "run1-proposal",
    "run2_proposal": "run2-proposal",
}

# (item id in this review, kind, run, record id, heading)
SAMPLE: list[tuple[str, str, str, str, str]] = [
    ("A1", "answer", "answer", "qa-berlin", "Question about one fact (qa)"),
    ("A2", "answer", "answer", "figure-handbook", "Question about a figure (qa, figure)"),
    ("A3", "answer", "answer", "summarize-release", "Summary (summarize): derived citations"),
    ("A4", "answer", "answer", "compare-depots-release", "Comparison of two files (compare)"),
    (
        "A5",
        "answer",
        "answer",
        "review-launch-plan",
        "Review of the open document (document_review)",
    ),
    ("A6", "answer", "answer", "diagram-release", "Diagram request: build and deploy"),
    (
        "A7",
        "historical-answer",
        "postfix_answer",
        "diagram-release",
        "Same request in the earlier run at default effort (routed summarize)",
    ),
    ("A8", "answer", "answer", "diagram-architecture", "Diagram of a figure"),
    ("P1", "proposal", "proposal", "edit-rewrite", "Edit: rewrite one paragraph"),
    (
        "P2",
        "proposal",
        "proposal",
        "edit-insert-from-release",
        "Edit: insert blocks drawn from a source",
    ),
    ("P3", "proposal", "proposal", "edit-delete", "Edit: delete a block"),
    ("P4", "proposal", "proposal", "edit-diagram", "Edit: insert a diagram"),
    (
        "P5",
        "proposal",
        "proposal",
        "edit-two-operations",
        "Edit: two operations (a tightened sentence and a note)",
    ),
    ("P6", "proposal", "proposal", "draft-proposal", "Draft: a new document from a source"),
    (
        "P7",
        "proposal",
        "proposal",
        "edit-end-summary",
        "Edit: a closing summary (the review's P7, re-run on the new build)",
    ),
    (
        "P7h",
        "historical-proposal",
        "run1_proposal",
        "edit-end-summary",
        "The review's P7 as it was in run 1 (repaired reply)",
    ),
    (
        "P8h",
        "historical-proposal",
        "run2_proposal",
        "edit-diagram",
        "The review's P8 as it was in run 2 (repaired reply)",
    ),
    (
        "Q1",
        "quality",
        "quality_dev",
        "closing-summary-atlas",
        "Closing summary of a document with several facts (dev)",
    ),
    ("Q2", "quality", "quality_heldout", "closing-summary-hiring", "Closing summary (held-out)"),
    (
        "Q3",
        "quality",
        "quality_dev",
        "rewrite-qualified-dev",
        "Rewrites that must keep qualifications (dev)",
    ),
    (
        "Q4",
        "quality",
        "quality_heldout",
        "rewrite-qualified-heldout",
        "Rewrites that must keep qualifications (held-out)",
    ),
    (
        "Q5",
        "quality",
        "quality_heldout",
        "diagram-onboarding-answer",
        "Diagram of steps whose order is not stated: answer (held-out)",
    ),
    (
        "Q6",
        "quality",
        "quality_heldout",
        "diagram-onboarding-edit",
        "Diagram of steps whose order is not stated: proposal (held-out)",
    ),
    (
        "Q7",
        "quality",
        "quality_heldout",
        "summarize-onboarding",
        "Summary with derived citations (held-out)",
    ),
    (
        "Q8",
        "quality",
        "quality_dev",
        "review-launch-risks",
        "Review wording: a stated effect (dev)",
    ),
    (
        "Q9",
        "quality",
        "quality_dev",
        "missing-context-review",
        "A request about the document that did not include it (dev)",
    ),
    (
        "Q10",
        "quality",
        "quality_heldout",
        "compare-handbook-scan",
        "Comparison that must address both selected files (held-out)",
    ),
    (
        "Q11",
        "quality",
        "quality_heldout",
        "draft-onboarding-welcome",
        "Draft that must fulfil its purpose (held-out)",
    ),
]

# Where an item exists in the earlier runs too, its earlier output is shown for comparison (build 0fe9a95, the one that was reviewed).
BASELINE_OF = {
    "A3": ("baseline_answer", "summarize-release"),
    "A5": ("baseline_answer", "review-launch-plan"),
    "A6": ("baseline_answer", "diagram-release"),
    "A8": ("baseline_answer", "diagram-architecture"),
    "P1": ("baseline_proposal", "edit-rewrite"),
    "P4": ("baseline_proposal", "edit-diagram"),
    "P5": ("baseline_proposal", "edit-two-operations"),
    "P6": ("baseline_proposal", "draft-proposal"),
    "P7": ("run1_proposal", "edit-end-summary"),
}

FIXTURES_OF = {
    "qa-berlin": ["depots.pdf"],
    "figure-handbook": ["handbook.docx", "architecture-figure.png"],
    "diagram-architecture": ["handbook.docx", "architecture-figure.png"],
    "summarize-release": ["release.md"],
    "compare-depots-release": ["depots.pdf", "release.md"],
    "review-launch-plan": ["launch-plan.md"],
    "diagram-release": ["release.md"],
    "edit-rewrite": ["launch-plan.md"],
    "edit-insert-from-release": ["launch-plan.md", "release.md"],
    "edit-delete": ["launch-plan.md"],
    "edit-diagram": ["launch-plan.md", "release.md"],
    "edit-two-operations": ["launch-plan.md"],
    "draft-proposal": ["release.md"],
    "edit-end-summary": ["launch-plan.md"],
    "closing-summary-atlas": ["atlas.md"],
    "closing-summary-hiring": ["hiring.md"],
    "rewrite-qualified-dev": ["qualified-dev.md"],
    "rewrite-qualified-heldout": ["qualified-heldout.md"],
    "diagram-onboarding-answer": ["onboarding.md"],
    "diagram-onboarding-edit": ["onboarding.md", "rollout.md"],
    "summarize-onboarding": ["onboarding.md"],
    "review-launch-risks": ["launch-plan.md"],
    "missing-context-review": ["release.md"],
    "compare-handbook-scan": ["handbook.docx", "march-scan-page-1.png"],
    "draft-onboarding-welcome": ["onboarding.md"],
}

# AGENT ASSESSMENTS: my own reading of each output, written by hand after the run and labelled as such in every item. Filled in
# `agent_assessments.py` next to this file so the builder stays a pure function of records + readings.
try:  # pragma: no cover - the readings file is written after the runs
    from tests.eval.agent_assessments import AGENT
except ImportError:  # pragma: no cover
    AGENT = {}


# ---- reading records -------------------------------------------------------------------------------------------------------


def load(run: str) -> dict[str, dict[str, Any]]:
    path = EVIDENCE / f"{RUNS[run]}.records.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {r["id"]: r for r in data["records"]}


def manifest_of(run: str) -> dict[str, Any]:
    path = EVIDENCE / f"{RUNS[run]}.manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in str(text).split("\n"))


def fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def node_text(node: Any) -> str:
    if isinstance(node, list):
        return " ".join(t for t in (node_text(n) for n in node) if t)
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("text"), str):
        return node["text"]
    return " ".join(t for t in (node_text(c) for c in node.get("content") or []) if t)


def render_node(node: dict[str, Any], depth: int = 0) -> list[str]:
    """One ProseMirror block as readable markdown. A diagram is its Mermaid source in a fence."""
    kind = node.get("type")
    pad = "  " * depth
    if kind == "heading":
        return [
            f"{'#' * min(6, int((node.get('attrs') or {}).get('level') or 1) + 3)} {node_text(node)}"
        ]
    if kind == "diagram":
        return [fence(node_text(node), "mermaid")]
    if kind in ("bulletList", "orderedList"):
        out = []
        for i, item in enumerate(node.get("content") or [], start=1):
            marker = "-" if kind == "bulletList" else f"{i}."
            first, *rest = [
                t for child in item.get("content") or [] for t in render_node(child, depth + 1)
            ] or [""]
            out.append(f"{pad}{marker} {first.strip()}")
            out += [f"{pad}  {r.strip()}" for r in rest]
        return out
    if kind == "table":
        rows = []
        for r in node.get("content") or []:
            rows.append(
                "| "
                + " | ".join(node_text(c).replace("|", "\\|") for c in r.get("content") or [])
                + " |"
            )
        if rows:
            rows.insert(1, "|" + "---|" * (rows[0].count("|") - 1))
        return rows
    if kind == "blockquote":
        return [f"> {t}" for child in node.get("content") or [] for t in render_node(child, depth)]
    text = node_text(node)
    marks = [m.get("type") for c in node.get("content") or [] for m in c.get("marks") or []]
    if text and "italic" in marks and kind == "paragraph":
        return [f"*{text}*"]
    return [text] if text else []


def render_blocks(nodes: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for n in nodes:
        lines += render_node(n)
        lines.append("")
    return "\n".join(lines).strip() or "(nothing)"


def cite_marks(nums: list[int]) -> str:
    return (
        ("  *(cites " + ", ".join(f"[{n}]" for n in nums) + ")*") if nums else "  *(no citation)*"
    )


def answer_blocks_md(answer: dict[str, Any], citations_by_n: dict[int, dict]) -> str:
    out: list[str] = []
    for b in answer.get("blocks", []):
        if b["type"] == "paragraph":
            out.append(b["text"] + cite_marks(b.get("citations") or []))
        elif b["type"] == "bullet_list":
            out += [f"- {it['text']}{cite_marks(it.get('citations') or [])}" for it in b["items"]]
        else:
            out.append(
                f"**Diagram: {b.get('title') or 'untitled'}** (render status: {b.get('render_status')}; cites {b.get('citations') or 'nothing'})"
            )
            out.append(fence(b["mermaid"], "mermaid"))
            if b.get("assumptions"):
                out.append(
                    "**Assumed, not stated in the sources** (shown with the diagram):\n"
                    + "\n".join(f"- {a}" for a in b["assumptions"])
                )
        out.append("")
    gaps = answer.get("evidence_gaps") or []
    if gaps:
        out.append(
            "**Not found in the sources** (the model's own list of gaps):\n"
            + "\n".join(f"- {g}" for g in gaps)
        )
    return "\n".join(out).strip()


def kv(rows: list[tuple[str, Any]]) -> str:
    return "\n".join(f"* **{k}:** {v}" for k, v in rows if v not in (None, "", []))


# ---- the sections of an item -------------------------------------------------------------------------------------------------


def sources_md(coverage: dict[str, Any], manifest: dict[str, Any] | None = None) -> str:
    rows = []
    versions = {s["source_version_id"]: s for s in (manifest or {}).get("sources", [])}
    for s in (coverage.get("sources") if isinstance(coverage.get("sources"), list) else []):
        rows.append(
            f"`{s['source_name']}` version {s.get('version_no')} (`{s.get('source_version_id')}`), read as **{s.get('mode')}**: "
            f"{s.get('sections_read')} of {s.get('sections_total')} sections"
            + (
                f", {s.get('passages_used')} passages used"
                if s.get("passages_used") is not None
                else ""
            )
            + (
                f", {s.get('figures_examined')} of {s.get('figures_total')} figures examined"
                if s.get("figures_total")
                else ""
            )
        )
    if not rows:
        for v in versions.values():
            rows.append(
                f"`{v['source_name']}` version {v.get('version_no')} (`{v['source_version_id']}`)"
            )
    return "\n".join(f"  * {r}" for r in rows) if rows else "  none selected"


def document_md(coverage: dict[str, Any], manifest: dict[str, Any] | None = None) -> str:
    d = coverage.get("document")
    m = (manifest or {}).get("document") or {}
    if not d and not m:
        return "none (no open document was read for this request)"
    d = d or {}
    ctx = d.get("context") or m.get("context") or {}
    bits = [
        f"document `{d.get('document_id') or m.get('document_id')}`",
        f"captured at update counter {d.get('captured_update_counter', m.get('yjs_update_counter'))}",
        f"scope `{d.get('scope') or (m.get('scope') if isinstance(m.get('scope'), str) else 'edit')}`",
        f"mode `{d.get('mode')}`",
    ]
    if m.get("content_hash"):
        bits.append(f"content hash `{m['content_hash'][:16]}…`")
    s = ", ".join(b for b in bits if b)
    if ctx:
        s += (
            f"; context shown to the model: **{ctx.get('mode')}** ({'complete' if ctx.get('complete') else 'PARTIAL'}), "
            f"{ctx.get('blocks_shown')} of {ctx.get('blocks_total')} blocks, {ctx.get('omitted_blocks', 0)} not shown, "
            f"{ctx.get('clipped_blocks', 0)} cut short"
        )
    return s


def passages_md(candidates: list[dict[str, Any]], strategy_note: str) -> str:
    if not candidates:
        return "unavailable: this run did not record the passages the request selected.\n"
    out = [strategy_note, ""]
    for c in candidates:
        place = []
        if c.get("page_start"):
            place.append(
                f"page {c['page_start']}"
                + (f"-{c['page_end']}" if c.get("page_end") not in (None, c["page_start"]) else "")
            )
        if c.get("figure_number"):
            place.append(f"figure {c['figure_number']}")
        if c.get("heading_path"):
            place.append(" > ".join(c["heading_path"]))
        if c.get("block_index") is not None:
            place.append(f"document block {c['block_index']}")
        head = f"**{c.get('label') or '#' + str(c.get('rank'))}** · rank {c.get('rank')} · `{c.get('source_name') or 'the open document'}`"
        head += (" · " + ", ".join(place)) if place else ""
        head += f" · extraction: {c.get('extraction')}" if c.get("extraction") else ""
        out.append(head)
        out.append(
            quote(c["text"])
            if c.get("text")
            else "> (the text of this section is reached through the citations below)"
        )
        out.append("")
    return "\n".join(out)


def citations_md(item_dir: str, result: dict[str, Any] | None, origins: dict[str, Any]) -> str:
    cites = (result or {}).get("citations") or []
    if not cites:
        return "This output cites nothing (no evidence label was used).\n"
    out = []
    for c in cites:
        n = str(c["n"])
        loc = c.get("locator") or {}
        where = []
        if loc.get("page_start"):
            where.append(
                f"page {loc['page_start']}"
                + (
                    f"-{loc['page_end']}"
                    if loc.get("page_end") not in (None, loc["page_start"])
                    else ""
                )
            )
        if loc.get("figure_number"):
            where.append(f"figure {loc['figure_number']}")
        if loc.get("heading_path"):
            where.append(" > ".join(loc["heading_path"]))
        out.append(
            f"#### [{c['n']}] `{c['label']}` · {c.get('kind')} · `{c.get('source_name') or 'the open document'}`"
            + (f" · {', '.join(where)}" if where else "")
        )
        derived = c.get("derived")
        out.append(
            kv(
                [
                    (
                        "What the reader sees as the excerpt",
                        (
                            "**DERIVED: a summary written by the model, shown as such, never as a quotation**"
                            if derived
                            else "text of the cited passage"
                        ),
                    ),
                    ("Extraction", c.get("extraction")),
                    (
                        "Machine-generated",
                        "yes (OCR, vision or figure description)"
                        if c.get("machine_generated")
                        else None,
                    ),
                    (
                        "Provenance recorded",
                        c.get("provenance", "unavailable (this run predates provenance records)"),
                    ),
                    (
                        "Derivation",
                        json.dumps(c.get("derivation")) if c.get("derivation") else None,
                    ),
                    (
                        "Validated quotations",
                        "; ".join(f"“{q}”" for q in c.get("quotes") or []) or None,
                    ),
                ]
            )
        )
        if c.get("excerpt"):
            out.append(
                (
                    "**Model-written summary** shown to the reader:\n"
                    if derived
                    else "Excerpt shown to the reader (the first 300 characters the system keeps):\n"
                )
                + quote(c["excerpt"])
            )
        got = origins.get(n)
        if got is None:
            out.append(
                "**Original evidence:** unavailable: this run did not open the citation's origin."
            )
        elif "origin" not in got:
            out.append(
                f"**Original evidence:** unavailable (the origin route answered {got.get('status')})."
            )
        else:
            for i, o in enumerate(got["origin"], start=1):
                if o["kind"] == "chunk":
                    pg = (
                        f"page {o['page_start']}"
                        + (
                            f"-{o['page_end']}"
                            if o.get("page_end") not in (None, o["page_start"])
                            else ""
                        )
                        if o.get("page_start")
                        else None
                    )
                    head = (
                        f"**Original text {i} of {len(got['origin'])}** · `{o.get('source_name')}` version {o.get('version_no')} · "
                        + ", ".join(
                            x
                            for x in (
                                pg,
                                " > ".join(o.get("heading_path") or []) or None,
                                f"part {o.get('ordinal', 0) + 1}",
                                f"extraction: {o.get('extraction')}",
                            )
                            if x
                        )
                        + f" · stored-text check: **{o.get('integrity')}**"
                    )
                    out.append(head)
                    out.append(quote(o["text"]) if o.get("text") else "> (text not available)")
                else:
                    out.append(
                        f"**Original text {i} of {len(got['origin'])}** · the open document block `{o.get('block_id')}` as captured (update {o.get('update_counter')}): **{o.get('integrity')}**"
                    )
                    out.append(quote(o["text"]) if o.get("text") else "> (text not available)")
        if c.get("figure_chunk_id"):
            out.append(
                "Original figure image: see `../fixtures/architecture-figure.png` (the figure this citation points to, generated by `tests/analysis_files.py`)."
            )
        out.append("")
    return "\n".join(out)


def automated_md(kind: str, rec: dict[str, Any]) -> str:
    rows: list[tuple[str, str, Any]] = []
    if kind in ("answer", "historical-answer"):
        rows += [
            ("Published", "the request produced a published answer", rec.get("published")),
            (
                "Route",
                "what the model's classification chose, and what the request finally ran as (fallback included)",
                json.dumps(
                    {
                        k: (rec.get("routing") or {}).get(k)
                        for k in ("model_route", "final_route", "fallback_used")
                    }
                ),
            ),
            (
                "required_phrase_coverage (phrase-v1)",
                "share of the item's required phrases found in the top-k evidence passages; a DIAGNOSTIC, NOT retrieval recall (a paraphrase scores 0; a phrase in an irrelevant passage scores 1)",
                json.dumps(rec.get("required_phrase_coverage") or rec.get("recall")),
            ),
            (
                "evidence (evidence-v1)",
                "gold evidence units (page / section / figure) covered by the top-ranked ORIGINAL candidates (recall@k) or by the sections/document read (coverage, unranked); see tests/eval/evidence.py",
                json.dumps(rec.get("evidence"))
                if rec.get("evidence")
                else "unavailable in this run",
            ),
            (
                "Citations without an invalid reference or dropped quote",
                "the system's own validation: every cited label exists, every quote occurs in its passage; does NOT require citing anything",
                rec.get("citation_clean"),
            ),
            (
                "Cites the expected source(s)",
                "string check on the cited source names",
                rec.get("cites_expected_sources"),
            ),
            (
                "Cited text contains the required phrase",
                "string check on the 300-character excerpts (a model-written summary can never contain a source phrase verbatim)",
                rec.get("cited_text_has_needles"),
            ),
            (
                "Derived citations open to verified original text",
                "STRUCTURE only: every derived citation has an origin whose stored text hash still matches; says nothing about support",
                rec.get("derived_citations_resolve"),
            ),
            (
                "Provenance incomplete (count)",
                "citations whose lineage cannot be followed to stored original text",
                rec.get("provenance_incomplete"),
            ),
            (
                "Diagram published / renders",
                "a diagram block exists / its Mermaid parses (SYNTAX only)",
                f"{rec.get('diagram_published')} / {rec.get('diagram_renders')}",
            ),
            (
                "Diagram draws directed relations / says they are assumed",
                "what the output SAID about order (its assumptions, gaps); cannot tell whether it is true",
                f"{rec.get('diagram_draws_order')} / {rec.get('diagram_order_disclosed')}",
            ),
            (
                "Latency (s) / failed provider calls",
                "end to end for this request / calls that did not succeed",
                f"{round(rec['latency_s'], 1) if rec.get('latency_s') else 'n/a'} / {(rec.get('ledger') or {}).get('failed_calls')}",
            ),
        ]
    elif kind in ("proposal", "historical-proposal"):
        rows += [
            ("Published", "the request produced a pending proposal", rec.get("published")),
            (
                "Valid on the first reply / repaired",
                "the first reply passed the shared document engine / one repair was used (codes of the first problems)",
                f"{rec.get('first_try_valid')} / {rec.get('repaired')} ({', '.join(rec.get('problems') or []) or 'none'})",
            ),
            (
                "Target blocks and anchors resolve",
                "the base hashes and anchors still match the captured document and a replay of the operations is valid",
                rec.get("resolves"),
            ),
            (
                "Citation validity",
                "share of operations or blocks whose citations are valid (not a check of support)",
                rec.get("citation_valid"),
            ),
            (
                "Qualification review flags",
                "the app's word-level review notes on rewrites (hedges, state of the work, negation, conditions, attribution, figures); a note, never a verdict",
                json.dumps(rec.get("review_flags"))
                if "review_flags" in rec
                else "unavailable in this run",
            ),
            (
                "Latency (s)",
                "end to end",
                round(rec["latency_s"], 1) if rec.get("latency_s") else "n/a",
            ),
        ]
    else:
        s = rec["score"]
        rows += [
            ("Finding", "the finding this item tests (tests/eval/quality.py)", rec["finding"]),
            (
                "Finding met",
                "this item against its finding's criterion, from the structural and semantic results below (NOT a human judgement)",
                s["finding_met"],
            ),
            ("Why", "the checks behind the verdict", "; ".join(s["explain"])),
            (
                "Structural",
                "valid, rendered, cited: says the output is VALID",
                json.dumps(s["structural"]),
            ),
            (
                "Semantic",
                "deterministic content checks against a written rubric: concepts present, figures not in the material, placeholder, order disclosed, labels possibly invented",
                json.dumps(s["semantic"], default=str),
            ),
        ]
    return "| Check | What it is | Result |\n|---|---|---|\n" + "\n".join(
        f"| {a} | {b} | {str(c).replace('|', '/')} |" for a, b, c in rows
    )


HUMAN = """| Field | Your assessment |
|---|---|
| Factual support: is every statement supported by the original text shown above? (supported / partly / unsupported / not applicable) | |
| Completeness: does it cover what was asked? (complete / partial / missing key points) | |
| Usefulness: would you use it? (1 poor ... 5 very useful) | |
{extra}| Notes | |"""
EDIT_SAFETY = "| Edit safety: would you apply it as is? (safe / needs changes / unsafe) and what would go wrong if applied | |\n| Meaning preserved: does the new text keep every qualification of the old (planned vs done, may vs will, numbers, conditions, who said it)? | |\n"


def agent_md(item_id: str) -> str:
    text = AGENT.get(item_id)
    if not text:
        return "*(not written: the reading is added by hand after the run)*"
    return "\n".join(f"* {line}" for line in text) if isinstance(text, (list, tuple)) else text


def fixtures_md(rid: str) -> str:
    names = FIXTURES_OF.get(rid, [])
    return "\n".join(f"* [`{n}`](../fixtures/{n})" for n in names) or "none"


# ---- one item -------------------------------------------------------------------------------------------------------------------


def render_item(
    item_id: str, kind: str, run: str, rid: str, heading: str, runs: dict[str, dict]
) -> str | None:
    rec = runs.get(run, {}).get(rid)
    if rec is None:
        return None
    historical = kind.startswith("historical")
    run_manifest = manifest_of(run)
    code = (run_manifest.get("code") or {}).get("commit", "unavailable")
    head = [f"# {item_id} · {heading}", ""]
    meta = kv(
        [
            ("Item", f"`{rid}`"),
            (
                "Run",
                f"`{RUNS[run]}`"
                + (
                    " (HISTORICAL: the build that was reviewed; some evidence was not recorded then)"
                    if historical
                    else ""
                ),
            ),
            ("Code of that run", f"`{code[:10]}`" if code != "unavailable" else "unavailable"),
            (
                "Request id",
                f"`{rec.get('request_id')}`"
                if rec.get("request_id")
                else "`not recorded` (this run did not keep it)",
            ),
            ("Outcome", rec.get("outcome")),
        ]
    )
    out = head + [meta, ""]
    result = rec.get("result") if isinstance(rec.get("result"), dict) else None
    coverage = (result or {}).get("coverage") or rec.get("coverage") or {}
    man = rec.get("manifest") or {}
    # ---- 1 the request
    gold_a = next((g for g in corpus.GOLD if g.id == rid), None)
    gold_p = next((g for g in PGOLD if g.id == rid), None)
    qi = next((i for i in quality.ITEMS if i.id == rid), None)
    instruction = rec.get("instruction") or (
        gold_a.instruction
        if gold_a
        else gold_p.instruction
        if gold_p
        else qi.instruction
        if qi
        else "unavailable"
    )
    out += [
        "## 1. The request",
        "",
        "**Instruction, exactly as sent:**",
        "",
        quote(instruction),
        "",
    ]
    routing = rec.get("routing") or coverage.get("routing")
    target = rec.get("target")
    out.append(
        kv(
            [
                (
                    "Operation",
                    "answer in chat"
                    if kind in ("answer", "historical-answer")
                    or (kind == "quality" and rec.get("kind") == "answer")
                    else (rec.get("kind") or ("edit" if "edit" in rid else "draft")),
                ),
                (
                    "Route",
                    f"the model chose `{routing.get('model_route')}`, the request ran as `{routing.get('final_route')}` (fallback used: {'yes' if routing.get('fallback_used') else 'no'}; hint `{routing.get('hint')}`)"
                    if routing
                    else None,
                ),
                ("Target of the edit", json.dumps(target) if target else None),
                ("Sources selected", None),
            ]
        )
    )
    out += [
        "* **Sources selected (names, immutable versions):**",
        sources_md(coverage, man),
        f"* **Open document:** {document_md(coverage, man)}",
        "",
    ]
    if coverage.get("notices"):
        out += [
            "**Context notices the request carried:**",
            *[f"* `{n['code']}`: {n['message']} {n['action']}" for n in coverage["notices"]],
            "",
        ]
    # ---- 2 what it read
    out += ["## 2. What the request read (original material)", ""]
    cands = rec.get("candidates") or []
    note = {
        "qa": "Ranked original passages (best first), recorded before the answer was generated. Full text.",
        "summarize": "The sections that were READ and summarized (no ranking: whole sections). Their original chunks are listed under each citation below.",
        "review": "The blocks of the open document that were sent, in order.",
    }.get(gold_a.task if gold_a and gold_a.task in ("summarize", "review") else "qa", "")
    out.append(
        passages_md(cands, note)
        if cands
        else (
            "unavailable: this record has no ranked candidates (earlier run). The sources are in `../fixtures/`; the reader can open the cited passages below."
            if historical
            else "See the citations below: each lists the original text it opens to."
        )
    )
    out += ["", "Fixture files for this item (synthetic):", fixtures_md(rid), ""]
    # ---- 3 the output
    out += ["## 3. The output, complete", ""]
    if kind in ("answer", "historical-answer") and result:
        out.append(answer_blocks_md(result["answer"], {c["n"]: c for c in result["citations"]}))
    elif kind in ("proposal", "historical-proposal"):
        pv = rec.get("preview") or {}
        out += proposal_md(rec, pv)
    elif kind == "quality":
        out.append(quality_output_md(rec))
    out += ["", ""]
    # ---- 4 citations
    out += ["## 4. Citations and what they open to", ""]
    out.append(
        citations_md(
            rid,
            result or ({"citations": rec.get("citations")} if rec.get("citations") else None),
            rec.get("citation_origins") or {},
        )
    )
    # ---- 5 automated results
    out += [
        "## 5. Automated results (mechanical; none is a judgement of truth, completeness or usefulness)",
        "",
        automated_md(kind if kind != "quality" else "quality", rec),
        "",
    ]
    # ---- the earlier output of the same item
    if item_id in BASELINE_OF and not historical:
        b_run, b_id = BASELINE_OF[item_id]
        b = runs.get(b_run, {}).get(b_id)
        if b:
            out += [
                f"## Earlier output of the same item (run `{RUNS[b_run]}`, the build that was reviewed)",
                "",
                earlier_md(b),
                "",
            ]
    # ---- 6, 7
    out += [
        "## 6. Agent assessment (my own reading, written by hand after the run; it is NOT a human review and it can be wrong)",
        "",
        agent_md(item_id),
        "",
    ]
    extra = (
        EDIT_SAFETY
        if kind in ("proposal", "historical-proposal")
        or (kind == "quality" and rec.get("kind") in ("edit", "draft"))
        else ""
    )
    out += [
        "## 7. Human review (leave blank until you assess it)",
        "",
        HUMAN.format(extra=extra),
        "",
    ]
    return "\n".join(out)


def proposal_md(rec: dict[str, Any], pv: dict[str, Any]) -> list[str]:
    out: list[str] = []
    res = rec.get("result") or {}
    if res:
        out += [f"**Summary the model gave:** {res['answer'].get('summary') or ''}", ""]
    if (rec.get("proposal") or {}).get("before_content"):
        blocks = rec["proposal"]["before_content"].get("blocks") or {}
        if blocks:
            out += [
                "**Target as captured (complete, before any change):**",
                "",
                render_blocks(list(blocks.values())),
                "",
            ]
    if pv.get("kind") == "draft":
        out += [
            f"**Draft title:** {pv.get('title')}",
            "",
            "**The complete draft:**",
            "",
            render_blocks((pv.get("document") or {}).get("content") or []),
            "",
        ]
        return out
    for c in pv.get("changes", []):
        out += [f"### Change {c['op'] + 1}: `{c['kind']}`", ""]
        if c.get("reason"):
            out.append(f"*The model's reason:* {c['reason']}")
        out.append(f"*Cites:* {', '.join(f'[{n}]' for n in c.get('citations') or []) or 'nothing'}")
        if c.get("review_flags"):
            out += [
                "",
                "**Review flags the app showed on this change** (a word-level note, not a verdict):",
                *[f"* `{f['code']}`: {f['detail']}" for f in c["review_flags"]],
            ]
        if c.get("assumptions"):
            out += [
                "",
                "**Assumed, not stated in the sources** (shown in the preview):",
                *[f"* {a}" for a in c["assumptions"]],
            ]
        out += [
            "",
            "**Before:**",
            "",
            render_blocks(c.get("before") or []),
            "",
            "**After:**",
            "",
            render_blocks(c.get("after") or []),
            "",
        ]
    return out


def quality_output_md(rec: dict[str, Any]) -> str:
    raw = rec.get("output_raw") or {}
    if rec["kind"] == "answer":
        res = {"answer": raw, "citations": rec.get("citations") or []}
        return (
            answer_blocks_md(raw, {c["n"]: c for c in res["citations"]})
            if raw
            else "(not published)"
        )
    pv = raw.get("preview") or {}
    lines = proposal_md({"result": {"answer": {"summary": raw.get("summary")}}, "proposal": {}}, pv)
    return "\n".join(lines)


def earlier_md(b: dict[str, Any]) -> str:
    res = b.get("result")
    if res and res.get("answer"):
        return answer_blocks_md(res["answer"], {c["n"]: c for c in res["citations"]})
    pv = b.get("preview") or {}
    return "\n".join(proposal_md({}, pv)) if pv else "(not recorded)"


# ---- fixtures and the index -------------------------------------------------------------------------------------------------------


def write_fixtures() -> list[str]:
    fx = OUT / "fixtures"
    fx.mkdir(parents=True, exist_ok=True)
    srcs = corpus.sources()
    files: dict[str, bytes] = {
        "architecture-figure.png": af.diagram_png(),
        "release.md": af.MARKDOWN_SAMPLE.encode(),
        "onboarding.md": quality.ONBOARDING_MD.encode(),
        "depots.pdf": srcs["depots.pdf"],
        "handbook.docx": srcs["handbook.docx"],
    }
    for name, items in quality.DOCUMENTS.items():
        files[f"{name}.md"] = "\n\n".join(
            ("#" * max(1, lvl) + " " if kind == "heading" else "") + text
            for kind, lvl, text in items
        ).encode()
    for i, lines in enumerate(corpus.SCAN_PAGES, start=1):
        import io

        buf = io.BytesIO()
        af.page_image(lines).resize((850, 1100)).save(buf, "PNG")
        files[f"march-scan-page-{i}.png"] = buf.getvalue()
    for name, data in files.items():
        (fx / name).write_bytes(data)
    (fx / "README.md").write_text(
        "# Fixtures\n\nEverything here is SYNTHETIC and generated by `backend/tests/analysis_files.py`, `backend/tests/eval/corpus.py` and "
        "`backend/tests/eval/quality.py`; none of it is real content. It is the original material the reviewed outputs were made from.\n\n"
        "* `architecture-figure.png`: the figure inside `handbook.docx`. Look at it before judging any statement about it: the boxes (Client, API, "
        "Worker, Database) are joined by **plain lines with no arrowhead**. The picture shows left-to-right placement and which boxes are joined; "
        "it does not show the direction of any relation.\n"
        "* `release.md`: names Build and Deploy and an owner for each, and says there are three gates. It never says which comes first, and it "
        "does not name the third gate.\n"
        "* `onboarding.md`: three items (Orientation, Payroll, Equipment) in no stated order.\n"
        "* `depots.pdf` (eight pages, one depot each), `march-scan-page-N.png` (the three scanned pages OCR read), `handbook.docx`.\n"
        "* `launch-plan.md`, `atlas.md`, `hiring.md`, `rollout.md`, `qualified-*.md`: the open documents the edit items work on.\n",
        encoding="utf-8",
    )
    return sorted(files)


def main() -> int:
    runs = {name: load(name) for name in RUNS}
    items_dir = OUT / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    write_fixtures()
    index: list[str] = []
    missing: list[str] = []
    for item_id, kind, run, rid, heading in SAMPLE:
        text = render_item(item_id, kind, run, rid, heading, runs)
        if text is None:
            missing.append(item_id)
            continue
        (items_dir / f"{item_id}.md").write_text(text, encoding="utf-8")
        rec = runs[run][rid]
        status = "published" if rec.get("published") else f"not published ({rec.get('outcome')})"
        met = ""
        if kind == "quality":
            met = " · finding met: " + ("yes" if rec["score"]["finding_met"] else "NO")
        index.append(
            f"| [{item_id}](items/{item_id}.md) | {heading} | `{RUNS[run]}` | {status}{met} | ☐ |"
        )
    sheet = SHEET_HEAD + "\n".join(index) + "\n" + SHEET_TAIL
    (OUT / "human-review-sheet.md").write_text(sheet, encoding="utf-8")
    print(f"wrote {len(index)} items to {OUT}; missing (no record): {missing or 'none'}")
    return 0 if not missing else 1


SHEET_HEAD = """# Human review sheet v2: real-provider outputs, with their original evidence

Generated by `backend/tests/eval/build_review_sheet_v2.py` from the committed run records in `docs/evidence/ai-eval/`; it calls no provider.
It answers the review of the first sheet (`../human-review-sheet.md`, kept unchanged). The material is SYNTHETIC (`fixtures/`).

**How to use it.** Open an item. Each item is one file with the complete material in the order a reviewer needs it: the request and its
context, the original passages the request read, the full output (an edit shows the complete target before and after), every citation with what it
**opens to** (the original text, locator and extraction method; a model-written summary is labelled as derived and is never shown as a quotation), the
automated results with their definitions, my own reading, and the empty human fields. Look at `fixtures/architecture-figure.png` before judging anything
said about the figure.

Four kinds of statement are kept apart and labelled in every item:

1. the **request, source text and output**: copied from the run records, not edited;
2. **automated results**: mechanical checks. They say an output is valid, cited, rendered, or contains named points; none says it is true, complete or useful;
3. **agent assessment**: my own reading, written by hand after the run. A reading, not a verdict, not a human review;
4. **human review**: empty. It is yours.

Where an earlier run did not record something, the item says `unavailable` and why; nothing is reconstructed.

| Item | What it is | Run | Result | Reviewed |
|---|---|---|---|---|
"""
SHEET_TAIL = """
`A7`, `P7h` and `P8h` are the outputs of the earlier runs that the first review found fault with, kept as they were. `P7` and `A6` are the same requests
run again on the new build. Items `Q1`-`Q11` come from the task-quality evaluation (dev and held-out splits are named in each item).

Run names: `new-*` are the runs of the build after the review follow-up; `baseline-quality` is the quality dataset run on the build that was reviewed
(`0fe9a95`); `none-answer`, `postfix-answer`, `postfix-proposal`, `run1-proposal`, `run2-proposal` are the earlier runs, unchanged.
"""

if __name__ == "__main__":
    sys.exit(main())
