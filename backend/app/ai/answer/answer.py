"""The structured answer: its schema, the evidence labels it may cite, and the server-side validation.

THE MODEL NEVER NAMES DATABASE IDS. Every piece of evidence given to the model gets a request-local
label: `P1..Pn` for retrieved passages, `S1..Sk` for summarized sections, `D1..Dm` for blocks of the
captured document, `C1..Cj` for chat messages. The model cites labels; the server maps a label back to
what it stands for. A label that does not exist in this request is simply invalid.

WHAT THE SERVER CHECKS (the model's output is untrusted, like every input):
  * the shape: blocks of type paragraph / bullet_list / diagram, sane lengths and counts;
  * each citation names an existing label;
  * a QUOTE must be a whitespace-normalized substring of the cited item's text, and quotes are
    allowed only on items whose text came from a text layer, DOCX or plain text. On OCR, vision and
    figure items a quote is dropped (the citation stays, marked machine-generated: a paraphrase of
    something a machine read);
  * a citation whose quote is NOT in the cited text is invalid and removed (a fabricated quote makes
    the whole citation untrustworthy);
  * a claim (a paragraph, or one bullet) left with no valid citation is flagged `unsupported`;
  * if more than 30 % of the claims lose support, the caller gets ONE repair attempt, then the request
    fails. The share is computed over claims, never over characters.
A citation shows which passage the answer REFERENCED. It does not prove that the passage supports the
claim; the UI says so.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_BLOCKS = 60
MAX_ITEMS = 40
MAX_TEXT = 4000
MAX_GAPS = 20
MAX_GAP_TEXT = 300
MAX_CITATIONS_PER_CLAIM = 8
MAX_QUOTE = 400
EXCERPT_LIMIT = 300  # what a published result keeps of a cited passage (retention rule)
UNSUPPORTED_REPAIR_SHARE = 0.30
QUOTABLE = {"text_layer", "docx", "plain"}  # extraction kinds a quote may be taken from
_LABEL = re.compile(r"^[PSDC][1-9][0-9]{0,3}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")

ANSWER_SCHEMA_NAME = "cited_answer"
_CITATION = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "quote": {"type": ["string", "null"]},
    },
    "required": ["ref", "quote"],
    "additionalProperties": False,
}
_ITEM = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "citations": {"type": "array", "items": _CITATION}},
    "required": ["text", "citations"],
    "additionalProperties": False,
}
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["paragraph", "bullet_list", "diagram"]},
                    "text": {"type": ["string", "null"]},
                    "items": {"type": ["array", "null"], "items": _ITEM},
                    "title": {"type": ["string", "null"]},
                    "mermaid": {"type": ["string", "null"]},
                    "citations": {"type": "array", "items": _CITATION},
                },
                "required": ["type", "text", "items", "title", "mermaid", "citations"],
                "additionalProperties": False,
            },
        },
        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["blocks", "evidence_gaps"],
    "additionalProperties": False,
}


class BadAnswer(ValueError):
    """The reply does not have the required shape (never partially accepted)."""


@dataclass
class EvidenceItem:
    label: str
    kind: str  # passage | section | document | chat
    text: str
    extraction: str = "plain"  # text_layer | tesseract | vision_ocr | docx | plain | vision
    source_version_id: uuid.UUID | None = None
    source_name: str = ""
    locator: dict[str, Any] = field(default_factory=dict)
    chunk_ids: list[uuid.UUID] = field(default_factory=list)
    figure_chunk_id: uuid.UUID | None = None
    low_agreement: bool = False
    is_figure: bool = False

    @property
    def machine_generated(self) -> bool:
        return self.extraction in {"tesseract", "vision_ocr", "vision"} or self.is_figure

    @property
    def quotable(self) -> bool:
        return self.extraction in QUOTABLE and not self.is_figure and self.kind != "section"


class EvidenceSet:
    """The labelled evidence of ONE request. Labels are assigned in order and never reused."""

    def __init__(self) -> None:
        self.items: dict[str, EvidenceItem] = {}
        self._counters: dict[str, int] = {}

    def add(self, prefix: str, **fields: Any) -> EvidenceItem:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        label = f"{prefix}{self._counters[prefix]}"
        item = EvidenceItem(label=label, **fields)
        self.items[label] = item
        return item

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class ClaimStats:
    claims: int = 0
    unsupported: int = 0
    invalid_citations: int = 0
    dropped_quotes: int = 0

    @property
    def unsupported_share(self) -> float:
        return (self.unsupported / self.claims) if self.claims else 0.0


@dataclass
class ValidatedAnswer:
    blocks: list[dict[str, Any]]
    citations: list[dict[str, Any]]  # the numbered citation table, referenced by blocks via `n`
    evidence_gaps: list[str]
    stats: ClaimStats
    diagrams: list[int]  # indexes into blocks of diagram blocks (for the Mermaid check)


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise BadAnswer("text must be a string")
    text = _CONTROL.sub("", value).strip()
    if len(text) > limit:
        raise BadAnswer("text too long")
    return text


def normalize(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


class CitationTable:
    """Resolves the citations of claims against ONE request's evidence, exactly as phase 1 does for answers
    and phase 2 does for proposals: labels are mapped server-side, quotes must be in the cited text and are
    dropped on OCR/vision/figure items, chat is context and never evidence. Builds the numbered citation
    table and the claim statistics."""

    def __init__(self, evidence: EvidenceSet) -> None:
        self.evidence = evidence
        self.table: list[dict[str, Any]] = []
        self.numbers: dict[str, int] = {}
        self.stats = ClaimStats()

    def resolve(self, citations: Any) -> list[int]:
        """Validate one claim's citations; returns citation-table numbers."""
        stats, table, numbers = self.stats, self.table, self.numbers
        if citations is None:
            citations = []
        if not isinstance(citations, list):
            raise BadAnswer("citations must be a list")
        out: list[int] = []
        for c in citations[:MAX_CITATIONS_PER_CLAIM]:
            if not isinstance(c, dict) or not isinstance(c.get("ref"), str):
                stats.invalid_citations += 1
                continue
            label = c["ref"].strip()
            item = self.evidence.items.get(label) if _LABEL.match(label) else None
            if item is None or item.kind == "chat":
                stats.invalid_citations += (
                    1  # unknown label (or chat, which is context, not evidence)
                )
                continue
            quote = c.get("quote")
            kept_quote: str | None = None
            if isinstance(quote, str) and quote.strip():
                q = normalize(_CONTROL.sub("", quote))[:MAX_QUOTE]
                if not item.quotable:
                    stats.dropped_quotes += (
                        1  # OCR, vision, figure, section: a paraphrase, no quote
                    )
                elif q and q in normalize(item.text):
                    kept_quote = q
                else:
                    stats.invalid_citations += (
                        1  # a quote that is not in the passage: untrustworthy
                    )
                    continue
            key = label
            if key not in numbers:
                numbers[key] = len(table) + 1
                table.append(
                    {
                        "n": numbers[key],
                        "label": label,
                        "kind": item.kind,
                        "source_version_id": str(item.source_version_id)
                        if item.source_version_id
                        else None,
                        "source_name": item.source_name,
                        "locator": item.locator,
                        "extraction": item.extraction,
                        "machine_generated": item.machine_generated,
                        "low_agreement": item.low_agreement,
                        "chunk_ids": [str(i) for i in item.chunk_ids],
                        "figure_chunk_id": str(item.figure_chunk_id)
                        if item.figure_chunk_id
                        else None,
                        "excerpt": item.text[:EXCERPT_LIMIT],
                        "quotes": [],
                    }
                )
            row = table[numbers[key] - 1]
            if kept_quote and kept_quote not in row["quotes"]:
                row["quotes"].append(kept_quote)
            if numbers[key] not in out:
                out.append(numbers[key])
        return out

    def claim(self, citations: Any) -> tuple[list[int], bool]:
        """A claim is a statement that needs support: its numbers, and whether it has none."""
        self.stats.claims += 1
        nums = self.resolve(citations)
        unsupported = not nums
        if unsupported:
            self.stats.unsupported += 1
        return nums, unsupported


def validate_answer(raw: Any, evidence: EvidenceSet) -> ValidatedAnswer:
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        raise BadAnswer("no blocks")
    if not 1 <= len(raw["blocks"]) <= MAX_BLOCKS:
        raise BadAnswer("wrong number of blocks")
    gaps_raw = raw.get("evidence_gaps", [])
    if not isinstance(gaps_raw, list) or len(gaps_raw) > MAX_GAPS:
        raise BadAnswer("bad evidence_gaps")
    gaps = [clean_text(g, MAX_GAP_TEXT) for g in gaps_raw if isinstance(g, str) and g.strip()]

    citations_table = CitationTable(evidence)
    stats = citations_table.stats
    table = citations_table.table
    claim = citations_table.claim

    blocks: list[dict[str, Any]] = []
    diagrams: list[int] = []
    for b in raw["blocks"]:
        if not isinstance(b, dict):
            raise BadAnswer("block must be an object")
        kind = b.get("type")
        if kind == "paragraph":
            text = clean_text(b.get("text"))
            if not text:
                raise BadAnswer("empty paragraph")
            nums, unsupported = claim(b.get("citations"))
            blocks.append(
                {"type": "paragraph", "text": text, "citations": nums, "unsupported": unsupported}
            )
        elif kind == "bullet_list":
            items_raw = b.get("items")
            if not isinstance(items_raw, list) or not 1 <= len(items_raw) <= MAX_ITEMS:
                raise BadAnswer("bad bullet list")
            items = []
            for it in items_raw:
                if not isinstance(it, dict):
                    raise BadAnswer("bad bullet")
                text = clean_text(it.get("text"), 1000)
                if not text:
                    raise BadAnswer("empty bullet")
                nums, unsupported = claim(it.get("citations"))
                items.append({"text": text, "citations": nums, "unsupported": unsupported})
            blocks.append({"type": "bullet_list", "items": items})
        elif kind == "diagram":
            title = clean_text(b.get("title") or "", 200)
            source = b.get("mermaid")
            if not isinstance(source, str) or not source.strip():
                raise BadAnswer("empty diagram")
            nums, unsupported = claim(b.get("citations"))
            diagrams.append(len(blocks))
            blocks.append(
                {
                    "type": "diagram",
                    "title": title,
                    "mermaid": source.replace("\r\n", "\n").strip("\n"),
                    "citations": nums,
                    "unsupported": unsupported,
                    "render_status": "unchecked",
                    "render_error": None,
                }
            )
        else:
            raise BadAnswer("unknown block type")
    return ValidatedAnswer(blocks, table, gaps, stats, diagrams)


def needs_repair(stats: ClaimStats) -> bool:
    """More than 30 % of the claims lost their support (or there is not a single supported claim)."""
    return stats.claims > 0 and (
        stats.unsupported_share > UNSUPPORTED_REPAIR_SHARE or stats.unsupported == stats.claims
    )


def render_plain_text(answer: ValidatedAnswer, limit: int = 4000, pointer: str = "") -> str:
    """The chat message body: a plain-text rendering of the answer, at most `limit` characters, ending
    with `pointer` (a short note that the full card has the citations) when it had to be cut."""
    lines: list[str] = []
    for block in answer.blocks:
        if block["type"] == "paragraph":
            lines.append(block["text"] + _marks(block["citations"]))
        elif block["type"] == "bullet_list":
            lines.extend(f"- {it['text']}{_marks(it['citations'])}" for it in block["items"])
        else:
            lines.append(f"[Diagram: {block['title'] or 'untitled'}]")
    if answer.evidence_gaps:
        lines.append("Not found in the sources: " + "; ".join(answer.evidence_gaps))
    text = "\n".join(lines).strip() or "(empty answer)"
    tail = pointer
    if len(text) + len(tail) + 1 <= limit:
        return text + (("\n" + tail) if tail else "")
    cut = limit - len(tail) - 2
    return text[: max(0, cut)].rstrip() + "…" + (("\n" + tail) if tail else "")


def _marks(nums: list[int]) -> str:
    return (" " + "".join(f"[{n}]" for n in nums)) if nums else " [no citation]"
