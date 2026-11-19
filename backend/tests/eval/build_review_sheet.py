"""Build the human review sheet from the committed real-run evidence.

    cd backend
    PYTHONPATH=. uv run python -m tests.eval.build_review_sheet

Reads ONLY `docs/evidence/ai-eval/*.records.json` (committed, sanitized) and the synthetic corpus, and writes
`docs/evidence/ai-eval/human-review-sheet.md`. It calls no provider.

The sheet keeps three kinds of statement apart, each under its own heading in every entry:
  * the REQUEST, SOURCE TEXT and OUTPUT: copied from the records, not edited;
  * AUTOMATED CHECKS: the harness's mechanical results (string and schema checks, never a judgement of truth or usefulness);
  * AGENT READING: my own reading of the output, written by hand below (AGENT_READING). It is a reading, not a verdict, and
    it is not a human review.
The HUMAN REVIEW fields are left empty for the reviewer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests import analysis_files as af
from tests.eval import corpus
from tests.eval.proposals import GOLD as PGOLD

EVIDENCE = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "ai-eval"
OUT = EVIDENCE / "human-review-sheet.md"

# (entry id, kind, evidence file, record id, heading)
ANSWERS = (
    ("A1", "answer", "none-answer", "qa-berlin", "Question about one fact (qa)"),
    ("A2", "answer", "none-answer", "figure-handbook", "Question about a figure (qa, figure)"),
    ("A3", "answer", "none-answer", "summarize-release", "Summary (summarize)"),
    ("A4", "answer", "none-answer", "compare-depots-release", "Comparison of two files (compare)"),
    (
        "A5",
        "answer",
        "none-answer",
        "review-launch-plan",
        "Review of the open document (document_review)",
    ),
    (
        "A6",
        "answer",
        "none-answer",
        "diagram-release",
        "Diagram request, classifier effort `none` (routed qa)",
    ),
    (
        "A7",
        "answer",
        "postfix-answer",
        "diagram-release",
        "Same request, default effort (routed summarize)",
    ),
    (
        "A8",
        "answer",
        "none-answer",
        "diagram-architecture",
        "Diagram of a figure (qa, figure, diagram)",
    ),
)
PROPOSALS = (
    ("P1", "proposal", "postfix-proposal", "edit-rewrite", "Edit: rewrite one paragraph"),
    (
        "P2",
        "proposal",
        "postfix-proposal",
        "edit-insert-from-release",
        "Edit: insert blocks drawn from a source",
    ),
    ("P3", "proposal", "postfix-proposal", "edit-delete", "Edit: delete a block"),
    ("P4", "proposal", "postfix-proposal", "edit-diagram", "Edit: insert a diagram"),
    (
        "P5",
        "proposal",
        "postfix-proposal",
        "edit-two-operations",
        "Edit: two operations in one proposal",
    ),
    ("P6", "proposal", "postfix-proposal", "draft-proposal", "Draft: a new document from a source"),
    (
        "P7",
        "proposal",
        "run1-proposal",
        "edit-end-summary",
        "REPAIRED in run 1: the first reply was rejected",
    ),
    (
        "P8",
        "proposal",
        "run2-proposal",
        "edit-diagram",
        "REPAIRED in run 2: the first reply was rejected",
    ),
)

# My own reading of each output. Written after reading the records; a reading, not a verdict, not a human review.
AGENT_READING = {
    "A1": "The sentence matches the cited passage (4200 is written 4,200). One claim, and the harness confirmed the quote. I found nothing unsupported.",
    "A2": "Matches the cited figure description (Client, API, Worker, Database, left to right). It says the description is machine-generated. The corpus generator draws exactly those four boxes in that order, so the reading is right for this synthetic figure.",
    "A3": "Each statement traces to a cited section, and the note that the third gate is not described is accurate. Short, not exhaustive (it leaves out the table). The cited texts are section summaries the model wrote, not source text: check the statements against the release.md text in Appendix A.",
    "A4": "Statements trace to the cited passages, but the depot side is thin: it summarises filler text that repeats on every depot page (morning checks, weekly summaries), so the comparison says little. The note that the third gate is unspecified is accurate. A reviewer may rate completeness low.",
    "A5": "Lists one risk, the document's own sentence, and says the document gives no likelihood, impact or mitigation. Supported, but it restates the document and does not touch the budget paragraph. Usefulness is for you to judge.",
    "A6": "Steps and owners match the three cited passages. The order Build then Deploy is an inference (the passages do not state an order), and this answer does not flag that; the default-effort answer (A7) does. The note about the third gate is accurate.",
    "A7": "The automated check scores this worse (the required phrase 'compile the sources' is not in the cited text, because the summarize route cites section summaries). On my reading the diagram itself is supported by the same facts, marks the third gate as unspecified, and says the order is not stated. The automated 'worse' may overstate any difference in answer quality. That is for you to judge.",
    "A8": "Client, API, Worker, Database in a line matches the cited figure description.",
    "P1": "Meaning preserved and one block replaced. 'is scheduled to take place at noon on Friday' is wordier than the original, so whether it is 'clearer' is arguable.",
    "P2": "Inserts a heading and two list items that match release.md (Alice owns Build, compile and run tests, Bob owns Deploy, restart one at a time). Where it lands depends on the anchor: check the placement in the preview.",
    "P3": "Deletes exactly the budget paragraph that was asked for and nothing else. A deletion is a destructive change until applied, so it needs an explicit approval.",
    "P4": "The diagram follows the source steps and owners. Same inference about order as A6. It parses and renders.",
    "P5": "Both operations stay inside the launch plan. 'Launch is Friday at noon.' drops 'scheduled', which turns a plan into a statement of fact; the risk note only restates the existing risk.",
    "P6": "Stays within the release notes and flags the third gate as unspecified instead of inventing it. 'A proposal to clarify the release process' is a loose reading of 'a short project proposal from the release notes'. Nine operations, all inside the new document.",
    "P7": "The first reply had the wrong number of operations; the repaired reply is a generic closing sentence ('the preceding discussion brings together the key points presented above') that says nothing; the model's own summary says the document content was not provided to it. It passed every automated check. I would call it unhelpful, not unsafe.",
    "P8": "The first reply carried HTML in a diagram (rejected by validation). The repaired diagram is plain Mermaid with owners and matches the source. It is the safe kind of repair: the unsafe content never reached the preview.",
}

SOURCE_TEXTS = (
    "## Appendix A: the synthetic source texts\n\n"
    "`release.md` (the Markdown source used by A3, A6, A7, P2, P4, P6, P7, P8), verbatim:\n\n```markdown\n"
    + af.MARKDOWN_SAMPLE.strip()
    + "\n```\n\n"
    "`handbook.docx` holds one embedded figure (A2, A8): four light-blue boxes, Client, API, Worker, Database, drawn left to right "
    "and joined by lines (generated by `tests/analysis_files.py` `diagram_png`).\n\n"
    "`depots.pdf` has eight pages, one depot each, with one exact fact per page (for example the Berlin depot handled 4200 parcels in "
    "May) and filler text that repeats on every page. `march-scan.pdf` is three scanned pages read by OCR.\n\n"
    "## Appendix B: the open document the edit and review items work on (the launch plan)\n\n"
    + "\n".join(
        f"* B{i}. {('#' * lvl + ' ') if kind == 'heading' else ''}{text}"
        for i, (kind, lvl, text) in enumerate(corpus.DOCUMENT_ITEMS, 1)
    )
    + "\n"
)


def load(name: str) -> dict[str, dict[str, Any]]:
    data = json.loads((EVIDENCE / f"{name}.records.json").read_text(encoding="utf-8"))
    return {r["id"]: r for r in data["records"]}


def clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tiptap_text(node: Any) -> str:
    if isinstance(node, list):
        return " | ".join(tiptap_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)
    if node.get("type") == "text":
        return node.get("text", "")
    inner = " ".join(tiptap_text(c) for c in node.get("content") or [])
    if node.get("type") == "diagram":
        return "[diagram] " + inner
    tag = node.get("type")
    return (
        f"[{tag}] {inner}"
        if tag in ("heading", "bulletList", "orderedList", "table", "diagram")
        else inner
    )


def yes(value: Any) -> str:
    return {True: "yes", False: "no", None: "n/a"}.get(value, str(value))


def answer_entry(
    entry: tuple[str, str, str, str, str], rec: dict[str, Any], gold: corpus.Gold
) -> list[str]:
    eid, _, src, rid, title = entry
    res = rec["result"]
    routing = rec.get("routing") or {}
    lines = [
        f"### {eid} · {title}",
        "",
        f"* **Item** `{rid}` · **Run** `{src}` · **Request id** `{rec['request_id']}`",
        f'* **Request:** "{gold.instruction}"',
        f"* **Sources selected:** {', '.join(gold.sources) or 'none (the open document is included)'}",
        f"* **Route:** the model chose `{routing.get('model_route')}`, the request ran as `{routing.get('final_route')}`"
        f" (fallback used: {yes(routing.get('fallback_used'))})",
        "",
        "**Passages the answer cited** (an `S` label is a section summary written by the model, not source text)",
        "",
    ]
    for c in res["citations"]:
        loc = c.get("locator") or {}
        where = (
            f"pages {loc['page_start']}-{loc['page_end']}"
            if loc.get("page_start") and loc.get("page_end") != loc.get("page_start")
            else f"page {loc['page_start']}"
            if loc.get("page_start")
            else " > ".join(loc.get("heading_path") or []) or loc.get("block_type") or "-"
        )
        lines.append(
            f"* `{c['label']}` {c['source_name']} ({where}; {c['kind']}): {clip(c.get('excerpt') or '', 260)}"
        )
    gaps = res["answer"].get("evidence_gaps") or []
    lines += ["", "**Answer as published**", ""]
    for b in res["answer"]["blocks"]:
        if b["type"] == "diagram":
            lines += ["```mermaid", b["mermaid"], "```"]
        elif b["type"] == "bullet_list":
            lines += [f"* {i['text']}" for i in b["items"]]
        else:
            lines += [f"> {b.get('text', '')}  *(cites {b.get('citations')})*"]
    if gaps:
        lines += ["", "Evidence gaps the answer states: " + " ".join(f"({g})" for g in gaps)]
    needles = (
        ", ".join(f'"{n}"' for n in gold.needles)
        or "none (summary, comparison and review items have no required phrase)"
    )
    lines += [
        "",
        "**Expected evidence (the gold item)**",
        "",
        f"* Required phrases in the retrieved evidence: {needles}",
        f"* Source(s) the answer should cite: {', '.join(gold.cites) or 'none (the open document)'}",
        "",
        "**Automated checks** (mechanical; from the harness; not a judgement of truth or usefulness)",
        "",
        f"* Published: {yes(rec['published'])} · citations without an invalid reference or dropped quote: {yes(rec['citation_clean'])}"
        f" · cites the expected source: {yes(rec['cites_expected_sources'])} · cited text contains the required phrase: {yes(rec['cited_text_has_needles'])}",
        f"* Retrieval recall@1/3/5: {'/'.join('n/a' if rec['recall'][k] is None else f'{rec["recall"][k]:.0%}' for k in ('@1', '@3', '@5'))} (share of required phrases found in the top k passages; n/a = the item has none)"
        f" · diagram published/renders: {yes(rec['diagram_published'])}/{yes(rec['diagram_renders'])} · latency {rec['latency_s']:.1f} s"
        f" · failed provider calls for this request: {rec['ledger']['failed_calls']}",
        "",
        "**Agent reading** (my own, written after reading the output; not a human review)",
        "",
        f"* {AGENT_READING[eid]}",
        "",
    ]
    return lines + review_fields(factual=True, edit=False)


def proposal_entry(
    entry: tuple[str, str, str, str, str], rec: dict[str, Any], gold: Any
) -> list[str]:
    eid, _, src, rid, title = entry
    pv = rec["preview"]
    lines = [
        f"### {eid} · {title}",
        "",
        f"* **Item** `{rid}` · **Run** `{src}` · **Request id** `{rec.get('request_id', 'not recorded')}`",
        f'* **Request:** "{rec["instruction"]}"',
        f"* **Sources selected:** {', '.join(gold.sources) or 'none'} · **Target:** "
        + (
            "a new document"
            if gold.kind == "draft"
            else (
                f"block(s) {[f'B{i + 1}' for i in gold.blocks]}"
                if gold.blocks
                else "the open document (insertion)"
            )
        ),
        "",
        "**Proposal as published** (what the preview shows before anyone applies it)",
        "",
        f"* Summary: {pv.get('summary')}",
    ]
    if pv["kind"] == "draft":
        lines += [
            f"* Title: {pv.get('title')}",
            f"* Document: {clip(tiptap_text(pv['document']), 900)}",
        ]
    for c in pv.get("changes", []):
        lines += [
            f'* Operation {c["op"] + 1}: `{c["kind"]}` · cites passages {c["citations"] or "none"} · model\'s reason: "{c["reason"]}"',
            f"  * before: {clip(tiptap_text(c['before']), 300) or '(nothing)'}",
            f"  * after: {clip(tiptap_text(c['after']), 400)}",
        ]
    lines += [
        "",
        "**Expected evidence:** the proposals set has no required phrases. Judge it against the instruction, the source text (Appendix A) and "
        "the target blocks (Appendix B). The record stores citation numbers, not the passages they point to.",
        "",
        "**Automated checks** (mechanical; from the harness; not a judgement of truth, usefulness or safety)",
        "",
        f"* Published: {yes(rec['published'])} · valid on the first reply: {yes(rec['first_try_valid'])} · repaired: {yes(rec['repaired'])}"
        f" ({', '.join(rec['problems']) or 'no problems recorded'}) · target blocks resolve: {yes(rec['resolves'])}"
        f" · citations valid: {yes(rec['citation_valid'])} · invalid citation references: {rec['invalid_citation_refs']}"
        f" · operations: {rec['operations']} · latency {rec['latency_s']:.1f} s",
        "",
        "**Agent reading** (my own, written after reading the output; not a human review)",
        "",
        f"* {AGENT_READING[eid]}",
        "",
    ]
    return lines + review_fields(
        factual=bool(any(c["citations"] for c in pv.get("changes", []))) or pv["kind"] == "draft",
        edit=True,
    )


def review_fields(*, factual: bool, edit: bool) -> list[str]:
    lines = [
        "**Human review** (leave blank until you assess it)",
        "",
        "| Field | Your assessment |",
        "|---|---|",
        "| Factual support: is every statement supported by the source text? (supported / partly / unsupported / not applicable) | |",
        "| Completeness: does it cover what was asked? (complete / partial / missing key points) | |",
        "| Usefulness: would you use it? (1 poor ... 5 very useful) | |",
    ]
    if edit:
        lines.append(
            "| Edit safety: would you apply it as is? (safe / needs changes / unsafe) and what would go wrong if applied | |"
        )
    lines += ["| Notes | |", ""]
    return lines


def main() -> None:
    gold = {g.id: g for g in corpus.GOLD}
    pgold = {g.id: g for g in PGOLD}
    cache: dict[str, dict[str, dict[str, Any]]] = {}

    def rec(name: str, rid: str) -> dict[str, Any]:
        if name not in cache:
            cache[name] = load(name)
        return cache[name][rid]

    out = [
        "# Human review sheet: real-provider outputs",
        "",
        "Generated by `backend/tests/eval/build_review_sheet.py` from the committed evidence in this folder; no provider was called.",
        "The text below is output of the real models (`gpt-6-luna`, with `gemini-embedding-2` retrieval) over a **synthetic** corpus.",
        "",
        "How to read an entry. Four things are kept apart, and only the last is yours to fill:",
        "",
        "1. The request, the cited passages and the output are copied from the run records.",
        "2. **Automated checks** are mechanical (a quote occurs in its passage, a phrase appears in the cited text, the schema is valid). They do not "
        "say whether an answer is true, complete or useful, or whether an edit is safe.",
        "3. **Agent reading** is my own reading, written by hand after reading each output. It is not a human review and it can be wrong.",
        "4. **Human review** is empty.",
        "",
        "Run names: `none-answer` is the answer evaluation with classifier effort `none` (2026-11-19, 20:18 UTC); `postfix-answer` and "
        "`postfix-proposal` are the earlier post-fix runs; `run1-proposal` and `run2-proposal` are the first two proposal runs, kept here because they are the "
        "only real runs in which a proposal needed a repair (the post-fix run needed none). Faults are never injected into real model responses: "
        "an item named `...-repaired` in the proposals set is a stub-mode scenario name, and in a real run it only counts as repaired if the model's first reply was rejected.",
        "",
        "Sample: 8 answers (each route, and `diagram-release` twice) and 8 proposals (each operation kind, and both real repairs). "
        "The full sets are in the `*.records.json` files.",
        "",
        "## Answers",
        "",
    ]
    for e in ANSWERS:
        out += answer_entry(e, rec(e[2], e[3]), gold[e[3]])
    out += ["## Proposals (edits and drafts)", ""]
    for e in PROPOSALS:
        out += proposal_entry(e, rec(e[2], e[3]), pgold[e[3]])
    out.append(SOURCE_TEXTS)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT.name}: {len(ANSWERS)} answers, {len(PROPOSALS)} proposals, {len(out)} lines")


if __name__ == "__main__":
    main()
