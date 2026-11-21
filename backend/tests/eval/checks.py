"""Deterministic checks of task quality, each with a name that says what it does and does not establish.

THE LINE BETWEEN STRUCTURE AND SEMANTICS. The schema, label, scope and render checks elsewhere say a reply is VALID. They say
nothing about whether it is GOOD: a closing summary can be a valid proposal and say nothing (P7), a diagram can render and draw
an order the source never states (A6). The checks here look at the CONTENT, with rules that are written down and small enough to
be argued with:

  concepts     a documented rubric per task: the facts a good output must carry, each as a short list of surface forms
               (regular expressions, case-insensitive). A concept is PRESENT when any form matches. This is a recall check on
               named points. It does not check that the concept is used correctly, so it is paired with the figure and
               negation checks below, and it never replaces reading the output.
  figures      numbers, amounts, times and dates in the output that appear nowhere in the material it was given
               (qualifiers.figures: '$40,000' and 'forty thousand dollars' are one figure). Catches an invented or altered number.
  placeholder  phrases that talk about a document without saying anything. A DIAGNOSTIC for one known failure, never evidence that
               a summary is good: a summary free of them can still be empty, which is why concepts decide.
  disclosure   a diagram or answer that says the order of steps is assumed or not stated: the model's own `assumptions` or its
               evidence gaps mention order, sequence or an assumption. A keyword check on what the model SAID; it cannot tell
               whether what it said is true.

None of this is a judge and none of it is a human review. Where a property needs judgement (is this summary faithful, is this
rewrite clearer) the evaluation records an AGENT ASSESSMENT written by hand, labelled as such, beside these results.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.ai.edit import qualifiers


@dataclass(frozen=True)
class Concept:
    """One named point a good output carries, as the surface forms that would show it (any one is enough)."""

    id: str
    any_of: tuple[str, ...]

    def present_in(self, text: str) -> bool:
        return any(re.search(p, text, re.IGNORECASE) for p in self.any_of)


def concepts_found(text: str, concepts: Iterable[Concept]) -> dict[str, Any]:
    """Which concepts of the rubric the text carries. `share` is None for an empty rubric."""
    cs = list(concepts)
    found = [c.id for c in cs if c.present_in(text)]
    missing = [c.id for c in cs if c.id not in found]
    return {
        "found": found,
        "missing": missing,
        "share": (len(found) / len(cs)) if cs else None,
        "of": len(cs),
    }


def unsupported_figures(output: str, material: Iterable[str]) -> list[str]:
    """Figures in the output that occur in none of the material it was given. A reading of numbers and times only."""
    seen: set[str] = set()
    for text in material:
        seen |= qualifiers.figures(text)
    return sorted(qualifiers.figures(output) - seen)


# generic sentences that gesture at a document without naming anything in it (the P7 output, and its relatives)
PLACEHOLDER_PATTERNS = (
    r"\bkey points (presented|discussed|outlined|covered)\b",
    r"\bpreceding discussion\b",
    r"\b(the )?(above|preceding|previous) (sections?|discussion|content|points)\b.{0,40}\b(bring|brings|summar)",
    r"\bin summary,? the (document|text|above)\b",
    r"\bcontent (was|is) not (provided|available)\b",
    r"\b(cannot|can't|could not|unable to) (see|access|summari[sz]e)\b.{0,30}\b(document|content)\b",
)


def placeholder_hits(text: str) -> list[str]:
    return [p for p in PLACEHOLDER_PATTERNS if re.search(p, text, re.IGNORECASE)]


_ORDER_WORDS = re.compile(
    r"\b(order|sequence|sequenc\w+|assum\w+|infer\w*|not (stated|specified|say|says|given|clear)|unspecified|unordered|first|before|after)\b",
    re.IGNORECASE,
)


def order_disclosed(assumptions: Iterable[str], gaps: Iterable[str], chat_text: str = "") -> bool:
    """Did the output say, in its own words, that an order it may draw is assumed or not stated? True when an assumption is
    listed, or a gap or the chat text mentions the order/sequence/assumption. Says what was DISCLOSED, not whether it is right."""
    if any(a.strip() for a in assumptions):
        return True
    return any(_ORDER_WORDS.search(t) for t in [*gaps, chat_text] if t)


_ARROW = re.compile(r"-->|==>|-\.->|--[ox>]")


def draws_order(mermaid: str) -> bool:
    """Does the diagram draw directed relations between nodes (an ordering) rather than only group them?"""
    return bool(_ARROW.search(mermaid or ""))


def node_labels(mermaid: str) -> list[str]:
    """The visible labels of nodes in a flowchart source: `A[Label]`, `A("Label")`, `A{Label}`, `A["Label"]`."""
    out = []
    for m in re.finditer(r"""[\w-]+\s*[\[\(\{]+\s*"?([^\]\)\}"]+)"?\s*[\]\)\}]+""", mermaid or ""):
        label = re.sub(
            r"\\n|<br\s*/?>", " ", m.group(1)
        )  # Mermaid's own line breaks inside a label
        out.append(" ".join(label.split()))
    return out


_DISCLAIMS = re.compile(
    r"(\bnot (provided|specified|stated|described|named|given|identified|known)\b|\bunspecified\b|\bunnamed\b|\bunknown\b|\bundefined\b|\btbd\b)",
    re.IGNORECASE,
)


def invented_names(mermaid: str, material: str, allowed: Iterable[str] = ()) -> list[str]:
    """Node labels of a diagram with a word the material never uses: a POSSIBLY invented step or stage. A label is flagged when
    any of its words of five letters or more occurs nowhere in the material (case-insensitive substring, so 'restarts' covers
    'restart'). Conservative on purpose: a harmless paraphrase can be flagged, so a flag is something to read, not a verdict.
    Labels listed in `allowed` (for example 'unspecified') are ignored."""
    low = material.lower()
    ok = {a.lower() for a in allowed}
    bad = []
    for label in node_labels(mermaid):
        if label.lower() in ok or _DISCLAIMS.search(label):
            continue  # a node that says "not provided" / "unspecified" names nothing: that is the honest way to show a gap
        words = re.findall(r"[a-z]{5,}", label.lower())
        if any(w not in low for w in words):
            bad.append(label)
    return bad
