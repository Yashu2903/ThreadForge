"""Scoring one quality item (tests/eval/quality.py) from what the request produced: pure, no provider, no database.

`score_item(item, out)` returns three kinds of result, kept apart so no one of them stands in for another:

    structural   the output exists and is well formed: published, the diagram parses/renders, citations are valid. These say
                 the output is VALID, nothing about whether it is good.
    semantic     the deterministic content checks (tests/eval/checks.py and the helpers in quality.py): rubric concepts present,
                 figures not found in the material, placeholder phrases, an order disclosed, a name possibly invented, an
                 effect wrongly called missing. Each is a rule written down; none is a judge and none is a human review.
    finding      the verdict of THIS item against the acceptance criterion of its finding (F1..F7), made only of the two
                 groups above, with the reasons in `explain`.

The agent assessments (a hand-written reading of an output) and the human review are never produced here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tests import analysis_files as af
from tests.eval import quality
from tests.eval.checks import (
    concepts_found,
    draws_order,
    invented_names,
    order_disclosed,
    placeholder_hits,
    unsupported_figures,
)
from tests.eval.quality import QItem


@dataclass
class Outputs:
    """Everything the checks may look at, extracted by the runner from the published result."""

    published: bool
    text: str = ""  # the human-visible text of the output: answer paragraphs, proposal blocks, draft document
    diagrams: list[str] = field(default_factory=list)  # Mermaid sources
    assumptions: list[str] = field(default_factory=list)  # the model's own list for its diagrams
    notes: list[str] = field(default_factory=list)  # server-added captions next to a diagram
    gaps: list[str] = field(default_factory=list)  # the model's evidence gaps
    chat_text: str = ""  # the chat message body
    citations: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notices: list[dict[str, Any]] = field(default_factory=list)  # coverage notices
    sent_document: bool = False  # did the request read the open document at all
    renders: bool | None = None  # every diagram parses (None: no diagram)
    derived_resolves: bool | None = (
        None  # every derived citation opens to verified original text (None: none derived)
    )
    changed: dict[int, tuple[str, str]] = field(
        default_factory=dict
    )  # edit: selected block index -> (before, after)
    review_flags: dict[int, list[dict[str, str]]] = field(
        default_factory=dict
    )  # edit: the app's flags per block index
    first_try_valid: bool | None = None
    repaired: bool | None = None


def material_text(item: QItem) -> list[str]:
    """The text of the material the output may legitimately draw figures from (the sources and the document it was given)."""
    out: list[str] = []
    for name in item.material:
        if name in quality.DOCUMENTS:
            out.append(quality.document_text(name))
        elif name == "release.md":
            out.append(af.MARKDOWN_SAMPLE)
        elif name == "onboarding.md":
            out.append(quality.ONBOARDING_MD)
    return out


def _visible(out: Outputs) -> str:
    return "\n".join([out.text, *out.notes])


def score_item(item: QItem, out: Outputs) -> dict[str, Any]:
    structural = {
        "published": out.published,
        "diagram_renders": out.renders,
        "first_try_valid": out.first_try_valid,
        "repaired": out.repaired,
        "provenance_incomplete": out.stats.get("provenance_incomplete"),
        "invalid_citations": out.stats.get("invalid_citations"),
    }
    semantic: dict[str, Any] = {}
    explain: list[str] = []
    text = _visible(out)
    if item.concepts:
        semantic["concepts"] = concepts_found(text, item.concepts) | {"needed": item.min_concepts}
    if item.material:
        semantic["unsupported_figures"] = unsupported_figures(text, material_text(item))
    semantic["placeholder_hits"] = placeholder_hits(text)
    if item.forbid_named_third_gate:
        semantic["names_third_gate"] = quality.names_third_gate(text + "\n" + "\n".join(out.gaps))
    ok: bool | None = None

    if not out.published:
        return _result(item, structural, semantic, False, ["the request did not publish"], out)

    f = item.finding
    if f == "F1":
        c = semantic["concepts"]
        ok = (
            len(c["found"]) >= item.min_concepts
            and not semantic["unsupported_figures"]
            and not semantic["placeholder_hits"]
        )
        explain += [
            f"concepts {len(c['found'])}/{c['of']} (need {item.min_concepts}), missing {c['missing']}"
        ]
        if semantic["unsupported_figures"]:
            explain.append(f"figures not in the document: {semantic['unsupported_figures']}")
        if semantic["placeholder_hits"]:
            explain.append("a placeholder sentence")
    elif f == "F2":
        codes = [n.get("code") for n in out.notices]
        ok = item.expect_notice in codes and not out.sent_document
        explain.append(f"notices {codes}; document read: {out.sent_document}")
    elif f == "F3":
        c = semantic.get("concepts")
        lineage = out.derived_resolves is True and not out.stats.get("provenance_incomplete")
        enough = c is None or len(c["found"]) >= item.min_concepts
        ok = lineage and enough
        explain.append(
            f"derived citations resolve: {out.derived_resolves}; provenance incomplete: {out.stats.get('provenance_incomplete')}"
        )
        if c is not None:
            explain.append(f"concepts {len(c['found'])}/{c['of']} (need {item.min_concepts})")
    elif f == "F4":
        verdicts = []
        for src in out.diagrams:
            drawn = draws_order(src)
            said = order_disclosed([*out.assumptions, *out.notes], out.gaps, out.chat_text)
            bad = invented_names(
                src,
                "\n".join(material_text(item)),
                allowed=("unspecified", "details not provided", "unknown"),
            )
            verdicts.append(
                {"draws_order": drawn, "order_disclosed": said, "possibly_invented_labels": bad}
            )
        semantic["diagrams"] = verdicts
        ok = (
            bool(verdicts)
            and all(
                (not v["draws_order"] or v["order_disclosed"]) and not v["possibly_invented_labels"]
                for v in verdicts
            )
            and not semantic.get("names_third_gate")
        )
        explain.append("no diagram was published" if not verdicts else f"diagram checks {verdicts}")
    elif f == "F5":
        losses, flagged = {}, {}
        for idx, (before, after) in out.changed.items():
            facts = item.block_facts.get(idx, ())
            got = concepts_found(after, facts)
            semantic.setdefault("blocks", {})[idx] = got | {
                "before": before,
                "after": after,
                "length_ratio": round(len(after) / max(1, len(before)), 2),
            }
            if got["missing"]:
                losses[idx] = got["missing"]
                flagged[idx] = bool(out.review_flags.get(idx))
        semantic["lost"] = {str(k): v for k, v in losses.items()}
        semantic["lost_and_flagged"] = {str(k): v for k, v in flagged.items()}
        ok = bool(out.changed) and not losses
        explain.append(
            f"{len(out.changed)} sentences changed; facts lost: {semantic['lost'] or 'none'}; flagged by the app: {semantic['lost_and_flagged'] or 'n/a'}"
        )
    elif f == "F6":
        c = semantic.get("concepts")
        cited = {x.get("source_name") for x in out.citations}
        both = set(item.expect_sources_cited) <= cited
        ok = (
            (c is None or len(c["found"]) >= item.min_concepts)
            and both
            and not semantic.get("names_third_gate")
            and not semantic.get("unsupported_figures")
        )
        semantic["cited_sources"] = sorted(s for s in cited if s)
        explain.append(
            f"cites {semantic['cited_sources']} (need {list(item.expect_sources_cited) or 'n/a'}); concepts {c and len(c['found'])}/{c and c['of']}"
        )
    elif f == "F7":
        c = semantic["concepts"]
        gaps_and_text = "\n".join([*out.gaps, out.text])
        semantic["claims_effect_missing"] = quality.claims_effect_missing(gaps_and_text)
        semantic["invented_risks"] = quality.invented_risks(out.text)
        ok = (
            len(c["found"]) >= item.min_concepts
            and not semantic["claims_effect_missing"]
            and not semantic["invented_risks"]
        )
        explain.append(
            f"concepts {len(c['found'])}/{c['of']}; effect wrongly called missing: {semantic['claims_effect_missing']}; invented risks: {semantic['invented_risks']}"
        )
    return _result(item, structural, semantic, bool(ok), explain, out)


def _result(
    item: QItem, structural: dict, semantic: dict, ok: bool, explain: list[str], out: Outputs
) -> dict[str, Any]:
    return {
        "id": item.id,
        "split": item.split,
        "finding": item.finding,
        "structural": structural,
        "semantic": semantic,
        "finding_met": ok,
        "explain": explain,
    }


def flag_recall(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Of the rewritten sentences that lost a fact of their rubric, how many did the app's own review flag mark? (F5's second part.)"""
    lost = flagged = 0
    for r in results:
        for was_flagged in (r["semantic"].get("lost_and_flagged") or {}).values():
            lost += 1
            flagged += 1 if was_flagged else 0
    return {"lost": lost, "flagged": flagged, "value": (flagged / lost) if lost else None}


def sentence_text(nodes: list[dict[str, Any]]) -> str:
    """Plain text of ProseMirror JSON nodes (a block's text, one space between blocks)."""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if isinstance(n.get("text"), str):
                parts.append(n["text"])
            for c in n.get("content") or []:
                walk(c)
            if n.get("content") and n.get("type") != "text":
                parts.append(" ")
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(nodes)
    return re.sub(r"\s+", " ", "".join(parts)).strip()
