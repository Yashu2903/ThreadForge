"""What the model may say when it proposes a change, and how that is checked on the server.

THE MODEL NEVER WRITES TO A DOCUMENT. It returns JSON that this module turns into typed operations
(docs/ai.md section 18): replace a block's text, replace a character range, insert new blocks after an anchor,
delete blocks. It names blocks by short labels (`B12`) that the server maps back to ids; it never sees or
emits an id, a hash, HTML or Yjs bytes. Everything it returns is untrusted input:

  shape       types, lengths and counts; an unknown operation, block type or mark is an error, not ignored;
  labels      a block label that was not shown to the model, or an anchor that was not offered, is an error;
  citations   resolved against the request's evidence bundle exactly as for answers (`CitationTable`);
  documents   the typed operations are then applied to the CAPTURED document by the shared TypeScript engine
              (packages/proposal-check) and the whole result must validate: scope, anchors, ranges, limits,
              links, diagrams, the editor schema.

Strict-mode JSON schemas want every property present, so optional fields are `null`able and required;
nesting is kept shallow on purpose (table cells, quote paragraphs and list items are plain strings; only
paragraph/heading text and replacement text carry marks) to stay within a provider's nesting limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.answer.answer import CitationTable, ClaimStats, EvidenceSet
from app.ai.edit.target import START, Linearized

MAX_OPERATIONS = 50
MAX_INSERTED_CHARS = 30_000
MAX_BLOCKS_PER_OP = 40
MAX_LIST_ITEMS = 60
MAX_TABLE_CELLS = 400
MAX_TEXT = 12_000
MAX_REASON = 400
MAX_SUMMARY = 600
MAX_TITLE = 200
MARK_TYPES = ("bold", "italic", "underline", "strike", "link")
BLOCK_TYPES = ("paragraph", "heading", "quote", "bullet_list", "numbered_list", "table", "diagram")
OPERATIONS = ("replace_block_text", "replace_range", "insert_blocks_after", "delete_blocks")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

EDIT_SCHEMA_NAME = "document_edit"
DRAFT_SCHEMA_NAME = "document_draft"

_CITATION = {
    "type": "object",
    "properties": {"ref": {"type": "string"}, "quote": {"type": ["string", "null"]}},
    "required": ["ref", "quote"],
    "additionalProperties": False,
}
_MARK = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(MARK_TYPES)},
        "href": {"type": ["string", "null"]},
    },
    "required": ["type", "href"],
    "additionalProperties": False,
}
_RUN = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "marks": {"type": "array", "items": _MARK}},
    "required": ["text", "marks"],
    "additionalProperties": False,
}
_ITEM = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "depth": {"type": "integer"}},
    "required": ["text", "depth"],
    "additionalProperties": False,
}
_BLOCK = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(BLOCK_TYPES)},
        "level": {"type": ["integer", "null"]},
        "inline": {"type": ["array", "null"], "items": _RUN},
        "paragraphs": {"type": ["array", "null"], "items": {"type": "string"}},
        "items": {"type": ["array", "null"], "items": _ITEM},
        "rows": {
            "type": ["array", "null"],
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "header_row": {"type": ["boolean", "null"]},
        "mermaid": {"type": ["string", "null"]},
        "citations": {"type": "array", "items": _CITATION},
    },
    "required": [
        "type",
        "level",
        "inline",
        "paragraphs",
        "items",
        "rows",
        "header_row",
        "mermaid",
        "citations",
    ],
    "additionalProperties": False,
}
EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPERATIONS)},
                    "block": {"type": ["string", "null"]},
                    "start": {"type": ["integer", "null"]},
                    "end": {"type": ["integer", "null"]},
                    "anchor": {"type": ["string", "null"]},
                    "block_refs": {"type": ["array", "null"], "items": {"type": "string"}},
                    "new_inline": {"type": ["array", "null"], "items": _RUN},
                    "blocks": {"type": ["array", "null"], "items": _BLOCK},
                    "reason": {"type": "string"},
                    "citations": {"type": "array", "items": _CITATION},
                },
                "required": [
                    "op",
                    "block",
                    "start",
                    "end",
                    "anchor",
                    "block_refs",
                    "new_inline",
                    "blocks",
                    "reason",
                    "citations",
                ],
                "additionalProperties": False,
            },
        },
        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "operations", "evidence_gaps"],
    "additionalProperties": False,
}
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "blocks": {"type": "array", "items": _BLOCK},
        "include_sources_section": {"type": ["boolean", "null"]},
        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "blocks", "include_sources_section", "evidence_gaps"],
    "additionalProperties": False,
}


class BadProposal(Exception):
    """The reply cannot be turned into valid operations. `errors` is fed back for the one repair attempt."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(errors[0]["message"] if errors else "invalid proposal")
        self.errors = errors


def problem(op: int | None, code: str, message: str) -> dict[str, Any]:
    return {"op": op, "code": code, "message": message}


def describe_problems(errors: list[dict[str, Any]]) -> str:
    """The repair note's text: what was wrong, by operation number (1-based, as the model counted them)."""
    out = []
    for e in errors[:8]:
        where = f"operation {e['op'] + 1}: " if e.get("op") is not None else ""
        out.append(f"{where}{e.get('code', 'invalid')} - {e.get('message', '')}")
    return "; ".join(out)


def _text(value: Any, limit: int, path: str, op: int | None, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise BadProposal([problem(op, "invalid_type", f"{path} must be text")])
    cleaned = _CONTROL.sub("", value.replace("\r\n", "\n"))
    if len(cleaned) > limit:
        raise BadProposal([problem(op, "too_long", f"{path} is longer than {limit} characters")])
    if not allow_empty and not cleaned.strip():
        raise BadProposal([problem(op, "empty", f"{path} must not be empty")])
    return cleaned


def normalize_runs(raw: Any, path: str, op: int | None) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise BadProposal([problem(op, "invalid_type", f"{path} must be a list of runs")])
    runs: list[dict[str, Any]] = []
    for run in raw:
        if not isinstance(run, dict):
            raise BadProposal(
                [problem(op, "invalid_type", f"{path} holds a run that is not an object")]
            )
        marks: list[dict[str, Any]] = []
        for mark in run.get("marks") or []:
            if not isinstance(mark, dict) or mark.get("type") not in MARK_TYPES:
                raise BadProposal(
                    [
                        problem(
                            op, "invalid_mark", "marks are bold, italic, underline, strike and link"
                        )
                    ]
                )
            entry: dict[str, Any] = {"type": mark["type"]}
            if mark["type"] == "link":
                entry["href"] = mark.get("href")
            marks.append(entry)
        runs.append({"text": _text(run.get("text"), MAX_TEXT, f"{path} text", op), "marks": marks})
    return runs


def _plain_runs(value: Any, path: str, op: int | None) -> list[dict[str, Any]]:
    return [{"text": _text(value, MAX_TEXT, path, op), "marks": []}]


def normalize_block(raw: Any, path: str, op: int | None) -> dict[str, Any]:
    """One new block, as the TypeScript engine wants it (table cells, quote paragraphs and list items
    become single plain runs)."""
    if not isinstance(raw, dict) or raw.get("type") not in BLOCK_TYPES:
        raise BadProposal(
            [problem(op, "invalid_block", f"{path}: a block is one of {', '.join(BLOCK_TYPES)}")]
        )
    kind = raw["type"]
    if kind == "paragraph":
        return {"type": "paragraph", "inline": normalize_runs(raw.get("inline"), path, op)}
    if kind == "heading":
        return {
            "type": "heading",
            "level": raw.get("level"),
            "inline": normalize_runs(raw.get("inline"), path, op),
        }
    if kind == "quote":
        paras = raw.get("paragraphs")
        if not isinstance(paras, list) or not 1 <= len(paras) <= 10:
            raise BadProposal(
                [problem(op, "invalid_block", f"{path}: a quote has 1 to 10 paragraphs")]
            )
        return {"type": "quote", "paragraphs": [_plain_runs(p, path, op) for p in paras]}
    if kind in ("bullet_list", "numbered_list"):
        items = raw.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_LIST_ITEMS:
            raise BadProposal(
                [problem(op, "invalid_block", f"{path}: a list has 1 to {MAX_LIST_ITEMS} items")]
            )
        out = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("depth"), int):
                raise BadProposal(
                    [problem(op, "invalid_block", f"{path}: a list item needs text and a depth")]
                )
            out.append({"inline": _plain_runs(item.get("text"), path, op), "depth": item["depth"]})
        return {"type": kind, "items": out}
    if kind == "table":
        rows = raw.get("rows")
        if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
            raise BadProposal(
                [problem(op, "invalid_block", f"{path}: a table needs rows of cells")]
            )
        if sum(len(r) for r in rows) > MAX_TABLE_CELLS:
            raise BadProposal(
                [problem(op, "table_too_big", f"{path}: at most {MAX_TABLE_CELLS} cells")]
            )
        return {
            "type": "table",
            "header_row": bool(raw.get("header_row")),
            "rows": [[_plain_runs(c, path, op) for c in row] for row in rows],
        }
    source = raw.get("mermaid")
    if not isinstance(source, str) or not source.strip():
        raise BadProposal(
            [problem(op, "invalid_block", f"{path}: a diagram needs its Mermaid source")]
        )
    return {"type": "diagram", "mermaid": _text(source, 40_000, f"{path} mermaid", op)}


@dataclass
class NormalizedEdit:
    summary: str
    operations: list[dict[str, Any]]  # the TypeScript engine's operations, with REAL block ids
    meta: list[dict[str, Any]]  # per operation: kind, reason, citations (numbers), unsupported
    stats: ClaimStats
    table: list[dict[str, Any]]  # the numbered citation table
    gaps: list[str]
    diagrams: list[tuple[int, str]] = field(default_factory=list)  # (operation index, source)


def _label(view: Linearized, ref: Any, op: int, what: str) -> str:
    if not isinstance(ref, str) or ref.strip() not in view.labels:
        raise BadProposal(
            [
                problem(
                    op,
                    "unknown_block",
                    f"{what} {ref!r} is not a block label that was shown to you",
                )
            ]
        )
    return view.labels[ref.strip()]


def normalize_edit(raw: Any, view: Linearized, evidence: EvidenceSet) -> NormalizedEdit:
    """Raw model output to typed operations. Raises BadProposal with every problem found in one pass
    (as far as the first structural error allows), so one repair can fix them together."""
    if not isinstance(raw, dict) or not isinstance(raw.get("operations"), list):
        raise BadProposal([problem(None, "no_operations", "reply with an operations list")])
    raw_ops = raw["operations"]
    if not 1 <= len(raw_ops) <= MAX_OPERATIONS:
        raise BadProposal(
            [problem(None, "wrong_operation_count", f"between 1 and {MAX_OPERATIONS} operations")]
        )
    summary = _text(raw.get("summary", ""), MAX_SUMMARY, "summary", None).strip()
    gaps_raw = raw.get("evidence_gaps") or []
    gaps = [_text(g, 300, "evidence_gaps", None).strip() for g in gaps_raw if isinstance(g, str)][
        :20
    ]
    has_evidence = any(not lb.startswith("C") for lb in evidence.items)
    table = CitationTable(evidence)

    operations: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    diagrams: list[tuple[int, str]] = []
    errors: list[dict[str, Any]] = []
    for i, op in enumerate(raw_ops):
        try:
            if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
                raise BadProposal(
                    [
                        problem(
                            i, "unknown_operation", f"operation is one of {', '.join(OPERATIONS)}"
                        )
                    ]
                )
            kind = op["op"]
            reason = _text(op.get("reason", ""), MAX_REASON, "reason", i).strip()
            built: dict[str, Any]
            claims: list[Any] = [op.get("citations")]
            if kind == "replace_block_text":
                built = {
                    "op": kind,
                    "block_id": _label(view, op.get("block"), i, "block"),
                    "new_inline": normalize_runs(op.get("new_inline"), "new_inline", i),
                }
            elif kind == "replace_range":
                if not isinstance(op.get("start"), int) or not isinstance(op.get("end"), int):
                    raise BadProposal(
                        [problem(i, "bad_range", "replace_range needs integer start and end")]
                    )
                built = {
                    "op": kind,
                    "block_id": _label(view, op.get("block"), i, "block"),
                    "start": op["start"],
                    "end": op["end"],
                    "new_inline": normalize_runs(op.get("new_inline"), "new_inline", i),
                }
            elif kind == "insert_blocks_after":
                anchor = op.get("anchor")
                anchor_id = START if anchor == START else _label(view, anchor, i, "anchor")
                blocks_raw = op.get("blocks")
                if (
                    not isinstance(blocks_raw, list)
                    or not 1 <= len(blocks_raw) <= MAX_BLOCKS_PER_OP
                ):
                    raise BadProposal(
                        [problem(i, "invalid_operation", f"insert 1 to {MAX_BLOCKS_PER_OP} blocks")]
                    )
                blocks = [normalize_block(b, f"blocks[{n}]", i) for n, b in enumerate(blocks_raw)]
                for n, b in enumerate(blocks):
                    if b["type"] == "diagram":
                        diagrams.append((i, b["mermaid"]))
                    claims.append(blocks_raw[n].get("citations"))
                built = {"op": kind, "anchor_block_id": anchor_id, "blocks": blocks}
            else:  # delete_blocks
                refs = op.get("block_refs")
                if not isinstance(refs, list) or not refs or len(refs) > 50:
                    raise BadProposal(
                        [problem(i, "invalid_operation", "delete_blocks needs block_refs")]
                    )
                built = {
                    "op": kind,
                    "block_ids": [_label(view, r, i, "block") for r in refs],
                }
                claims = []  # deleting says nothing that needs support
            nums: list[int] = []
            unsupported = False
            if claims and has_evidence:
                # one claim per operation: an operation counts as supported if ANY of its citations holds
                merged: list[Any] = []
                for c in claims:
                    merged.extend(c if isinstance(c, list) else [])
                nums, unsupported = table.claim(merged)
            operations.append(built)
            meta.append(
                {"op": kind, "reason": reason, "citations": nums, "unsupported": unsupported}
            )
        except BadProposal as bad:
            errors.extend(bad.errors)
            operations.append({})
            meta.append({})
    if errors:
        raise BadProposal(errors)
    return NormalizedEdit(summary, operations, meta, table.stats, table.table, gaps, diagrams)


@dataclass
class NormalizedDraft:
    title: str
    summary: str
    blocks: list[dict[str, Any]]
    meta: list[dict[str, Any]]
    stats: ClaimStats
    table: list[dict[str, Any]]
    gaps: list[str]
    include_sources_section: bool
    diagrams: list[tuple[int, str]] = field(default_factory=list)  # (block index, source)


def clean_title(value: Any) -> str:
    title = _CONTROL.sub("", value if isinstance(value, str) else "").replace("\n", " ").strip()
    title = re.sub(r"\s+", " ", title)
    if not title:
        raise BadProposal([problem(None, "invalid_title", "the draft needs a title")])
    return title[:MAX_TITLE].rstrip()


def normalize_draft(raw: Any, evidence: EvidenceSet) -> NormalizedDraft:
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        raise BadProposal([problem(None, "no_blocks", "reply with a title and a blocks list")])
    blocks_raw = raw["blocks"]
    if not 1 <= len(blocks_raw) <= 120:
        raise BadProposal([problem(None, "wrong_block_count", "between 1 and 120 blocks")])
    title = clean_title(raw.get("title"))
    summary = _text(raw.get("summary", ""), MAX_SUMMARY, "summary", None).strip()
    gaps = [
        _text(g, 300, "evidence_gaps", None).strip()
        for g in (raw.get("evidence_gaps") or [])
        if isinstance(g, str)
    ][:20]
    table = CitationTable(evidence)
    blocks: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    diagrams: list[tuple[int, str]] = []
    errors: list[dict[str, Any]] = []
    for n, b in enumerate(blocks_raw):
        try:
            block = normalize_block(b, f"blocks[{n}]", n)
            blocks.append(block)
            if block["type"] == "heading":
                meta.append({"citations": [], "unsupported": False})
                continue
            nums, unsupported = table.claim(b.get("citations"))
            if block["type"] == "diagram":
                diagrams.append((n, block["mermaid"]))
            meta.append({"citations": nums, "unsupported": unsupported})
        except BadProposal as bad:
            errors.extend(bad.errors)
            blocks.append({})
            meta.append({})
    if errors:
        raise BadProposal(errors)
    include = raw.get("include_sources_section")
    return NormalizedDraft(
        title,
        summary,
        blocks,
        meta,
        table.stats,
        table.table,
        gaps,
        True if include is None else bool(include),
        diagrams,
    )


def sources_section(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The "Sources" heading and list the server appends to a draft: the files and locators ACTUALLY cited
    (from the validated citation table), never what the model says it used."""
    seen: dict[str, None] = {}
    for c in table:
        if c.get("kind") not in ("passage", "section") or not c.get("source_name"):
            continue
        loc = c.get("locator") or {}
        where = ""
        if loc.get("page_start"):
            end = loc.get("page_end")
            where = f", p. {loc['page_start']}" + (
                f"-{end}" if end and end != loc["page_start"] else ""
            )
        elif loc.get("heading_path"):
            where = ", " + " > ".join(str(h) for h in loc["heading_path"])
        seen[f"{c['source_name']}{where}"] = None
    if not seen:
        return []
    return [
        {"type": "heading", "level": 2, "inline": [{"text": "Sources", "marks": []}]},
        {
            "type": "bullet_list",
            "items": [{"inline": [{"text": line, "marks": []}], "depth": 0} for line in seen],
        },
    ]
