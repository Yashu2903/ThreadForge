"""The target of an edit request: what the person pointed at, resolved against the captured document.

NATURAL LANGUAGE NEVER CHOOSES THE TARGET. The composer sends the target explicitly:
  * blocks      ids of the blocks the person selected, plus the client's hash over them (the Phase 1 selection
                hash, see capture.selection_hash);
  * ranges      `(block_id, start, end, text_hash)`: a character range inside ONE block, in UTF-16 units (see
                units.py), and the hash of the text the client saw there;
  * insert_at   "start" or "end": insert at the top or the bottom of the document (the only way to ask for
                an insertion without selecting anything).
An edit request with none of the three has NO identifiable target and is refused with `target_required`
("select blocks or a range"): the model is never asked to guess where to write.

At capture the target is resolved against the LIVE state the collaboration service reports. A selected
block that is gone, a hash that no longer matches (the text changed after the person selected it) or a range
that no longer resolves fails with `selection_changed` and asks them to select again.

WHAT IS RECORDED (and copied into the proposal, so nothing depends on a snapshot that is pruned later):
  * `scope_ids`     the selected blocks AND everything inside them: the only ids operations may touch;
  * `ranges`        the declared ranges: `replace_range` must stay inside one of them;
  * `anchors`       where new blocks may be inserted: the TOP-LEVEL block that contains each selected block (a
                    new block is always a sibling at the top level of the document), "start" and/or the last
                    top-level block for "end";
  * `base_hashes`   block hash of every block in scope (the shared package's `blockHash`: content only, ids
                    ignored), checked again at apply time;
  * `before`        a copy of each outermost selected block as it was, with its children (the "before" side of
                    the preview, and what a conflict is explained from);
  * `counter`       the `yjs_update_counter` of the stored state the capture describes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai.answer.capture import Captured, CaptureFailed, DocBlock, resolve_selection
from app.ai.edit.units import RangeError, block_units, holds_text, text_hash, utf16_slice

START = "start"
END = "end"
MAX_SELECTED_BLOCKS = 200
MAX_RANGES = 20


@dataclass(frozen=True)
class RangeSel:
    block_id: str
    start: int
    end: int
    text_hash: str

    def to_json(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "start": self.start,
            "end": self.end,
            "text_hash": self.text_hash,
        }


@dataclass
class EditTarget:
    document_id: str
    counter: int
    content_hash: str
    block_ids: list[str]  # outermost selected blocks, in document order
    ranges: list[RangeSel]
    insert_at: str | None
    scope_ids: set[str]  # every block the model is shown (whole blocks and range-only blocks)
    whole_ids: set[
        str
    ]  # selected blocks and everything inside them: replace/delete may touch these
    anchors: list[str]  # "start" and/or top-level block ids, in document order ("start" first)
    base_hashes: dict[str, str]
    before: dict[str, dict[str, Any]] = field(repr=False)  # block id -> node JSON copy
    range_text: dict[str, str] = field(default_factory=dict, repr=False)  # "id:start:end" -> text

    def scope_json(self) -> dict[str, Any]:
        """What is stored in ai_proposals.scope (no text: the before copy holds the content)."""
        return {
            "block_ids": self.block_ids,
            "ranges": [r.to_json() for r in self.ranges],
            "insert_at": self.insert_at,
            "scope_ids": sorted(self.scope_ids),
            "whole_ids": sorted(self.whole_ids),
            "anchors": self.anchors,
        }

    def before_json(self) -> dict[str, Any]:
        return {"blocks": self.before, "range_text": self.range_text}


def find_node(
    content: dict[str, Any], block_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The node with this id and the list of its ancestors (outermost first, the doc excluded)."""

    def walk(
        nodes: Any, path: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            if (node.get("attrs") or {}).get("id") == block_id:
                return node, path
            hit = walk(node.get("content"), [*path, node])
            if hit:
                return hit
        return None

    found = walk(content.get("content"), [])
    if found is None:
        raise CaptureFailed("selection_changed")
    return found


def _descendant_ids(blocks: list[DocBlock], root: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for b in blocks:
        if b.parent_id:
            children.setdefault(b.parent_id, []).append(b.id)
    out, stack = {root}, [root]
    while stack:
        for child in children.get(stack.pop(), []):
            out.add(child)
            stack.append(child)
    return out


def resolve_edit_target(
    captured: Captured,
    *,
    block_ids: list[str],
    selection_hash: str | None,
    ranges: list[dict[str, Any]],
    insert_at: str | None,
    skip_hash: bool = False,
) -> EditTarget:
    """The target, or CaptureFailed('target_required' | 'selection_changed')."""
    if not block_ids and not ranges and insert_at is None:
        raise CaptureFailed("target_required")
    if len(block_ids) > MAX_SELECTED_BLOCKS or len(ranges) > MAX_RANGES:
        raise CaptureFailed("selection_too_large")
    if insert_at not in (None, START, END):
        raise CaptureFailed("target_required")
    by_id = {b.id: b for b in captured.blocks}
    order = {b.id: i for i, b in enumerate(captured.blocks)}

    selected: list[DocBlock] = []
    if block_ids:
        selected = resolve_selection(captured, block_ids, selection_hash, skip_hash=skip_hash)

    parsed: list[RangeSel] = []
    range_text: dict[str, str] = {}
    for raw in ranges:
        try:
            sel = RangeSel(
                str(raw["block_id"]), int(raw["start"]), int(raw["end"]), str(raw["text_hash"])
            )
        except (KeyError, TypeError, ValueError):
            raise CaptureFailed("selection_changed") from None
        if sel.block_id not in by_id:
            raise CaptureFailed("selection_changed")
        node, _ = find_node(captured.content, sel.block_id)
        if not holds_text(node):
            raise CaptureFailed("selection_changed")
        try:
            text = utf16_slice(block_units(node), sel.start, sel.end)
        except RangeError:
            raise CaptureFailed("selection_changed") from None
        if sel.start == sel.end or text_hash(text) != sel.text_hash:
            raise CaptureFailed("selection_changed")
        parsed.append(sel)
        range_text[f"{sel.block_id}:{sel.start}:{sel.end}"] = text
    parsed.sort(key=lambda r: (order[r.block_id], r.start))
    for a, b in zip(parsed, parsed[1:], strict=False):
        if a.block_id == b.block_id and b.start < a.end:
            raise CaptureFailed("selection_changed")  # overlapping ranges are not a selection

    # the scope: whole selected blocks with everything inside them; a range's block (alone)
    scope_ids: set[str] = set()
    for b in selected:
        scope_ids |= _descendant_ids(captured.blocks, b.id)
    for r in parsed:
        scope_ids.add(r.block_id)
    top_level_of = _top_level(captured)
    anchors: list[str] = []
    if insert_at == START:
        anchors.append(START)
    anchor_ids = {top_level_of[i] for i in scope_ids if i in top_level_of}
    if insert_at == END:
        tops = [b.id for b in captured.blocks if b.parent_id is None]
        if tops:
            anchor_ids.add(tops[-1])
    anchors += sorted(anchor_ids, key=lambda i: order[i])

    # a block both inside a selected container and range-selected stays "whole block" territory
    whole = {i for b in selected for i in _descendant_ids(captured.blocks, b.id)}
    before: dict[str, dict[str, Any]] = {}
    for b in selected:
        before[b.id] = find_node(captured.content, b.id)[0]
    for r in parsed:
        if r.block_id not in whole and r.block_id not in before:
            before[r.block_id] = find_node(captured.content, r.block_id)[0]

    base_hashes = {i: by_id[i].hash for i in sorted(scope_ids, key=lambda i: order[i])}
    return EditTarget(
        document_id=str(captured.document_id),
        counter=captured.counter,
        content_hash=captured.content_hash,
        block_ids=[b.id for b in selected],
        ranges=parsed,
        insert_at=insert_at,
        scope_ids=scope_ids,
        whole_ids=whole,
        anchors=anchors,
        base_hashes=base_hashes,
        before=before,
        range_text=range_text,
    )


def _top_level(captured: Captured) -> dict[str, str]:
    """Block id -> the id of the top-level block that contains it (itself, if it is top level)."""
    by_id = {b.id: b for b in captured.blocks}
    out: dict[str, str] = {}
    for b in captured.blocks:
        cur = b
        while cur.parent_id and cur.parent_id in by_id:
            cur = by_id[cur.parent_id]
        out[b.id] = cur.id
    return out


# ---- what the model is shown ------------------------------------------------------------------------------


@dataclass
class ContextDecision:
    """What the model was shown of the rest of the document, and why (recorded in the manifest and the
    coverage statement, so a reader can tell a complete read from a partial one).

    mode  full     every block with text is shown (blocks in the changeable scope are listed under CHANGEABLE BLOCKS)
          clipped  every block is shown but long ones are cut to CLIP_CHARS characters
          sampled  headings plus an even spread of the other blocks (the first and the last always kept)
          none     the document has no text, so there is nothing to show
    """

    mode: str
    blocks_total: int  # blocks with text (leaves)
    blocks_shown: (
        int  # blocks whose text (all, or the start of it) is in the prompt, scope blocks included
    )
    chars_shown: int
    tokens_shown: int
    budget_tokens: int
    clipped_blocks: int = 0
    omitted_blocks: int = 0

    @property
    def complete(self) -> bool:
        return self.mode in ("full", "none")

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "complete": self.complete,
            "blocks_total": self.blocks_total,
            "blocks_shown": self.blocks_shown,
            "chars_shown": self.chars_shown,
            "tokens_shown": self.tokens_shown,
            "budget_tokens": self.budget_tokens,
            "clipped_blocks": self.clipped_blocks,
            "omitted_blocks": self.omitted_blocks,
        }


@dataclass
class Linearized:
    labels: dict[str, str]  # "B12" -> block id
    scope_lines: list[str]  # "[B12] (paragraph) text" for every block in scope, in order
    toc_lines: list[str]  # headings of the whole document, for orientation in a large document
    anchor_labels: list[str]  # labels the model may use as insertion anchors ("start" is literal)
    # the READ-ONLY rest of the document: "(B7 paragraph) text". Deliberately not the "[B7] (kind)" shape of
    # the changeable blocks, so nothing here can be mistaken for something the model may change.
    context_lines: list[str] = field(default_factory=list)
    context: ContextDecision | None = None


CLIP_CHARS = 400


def label_for(index: int) -> str:
    return f"B{index + 1}"


def linearize(
    captured: Captured,
    target: EditTarget,
    *,
    toc_when_blocks_over: int = 40,
    context_budget_tokens: int | None = None,
    measure: Callable[[str], int] | None = None,
) -> Linearized:
    """Short labels instead of UUIDs: the model never sees or emits an id, a hash, HTML or Yjs bytes.

    Labels number EVERY block with an id in document order, so a label means the same thing in the table of
    contents and in the scope. The server maps labels back to ids; a label that is not in the map is an
    out-of-scope reference and is refused by validation.

    With a `context_budget_tokens`, the rest of the document is added as READ-ONLY context (see
    `document_context`). It never widens the scope: only the CHANGEABLE BLOCKS can be touched, whatever the
    context says.
    """
    labels = {label_for(i): b.id for i, b in enumerate(captured.blocks)}
    reverse = {v: k for k, v in labels.items()}
    toc = []
    if len(captured.blocks) > toc_when_blocks_over:
        for b in captured.blocks:
            if b.type == "heading":
                toc.append(f"[{reverse[b.id]}] (heading {b.level or 1}) {_clip(b.text, 120)}")
    scope_lines: list[str] = []
    for b in captured.blocks:
        if b.id not in target.scope_ids:
            continue
        node = find_node(captured.content, b.id)[0]
        kind = f"heading {b.level or 1}" if b.type == "heading" else b.type
        if holds_text(node):
            scope_lines.append(f"[{reverse[b.id]}] ({kind}) {block_units(node)}")
        else:
            scope_lines.append(f"[{reverse[b.id]}] ({kind})")
    anchor_labels = [reverse[a] for a in target.anchors if a != START]
    out = Linearized(labels, scope_lines, toc, anchor_labels)
    if context_budget_tokens is not None:
        out.context_lines, out.context = document_context(
            captured, target, reverse, context_budget_tokens, measure or _rough_tokens
        )
    return out


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def document_context(
    captured: Captured,
    target: EditTarget,
    reverse: dict[str, str],
    budget_tokens: int,
    measure: Callable[[str], int],
) -> tuple[list[str], ContextDecision]:
    """The whole document as read-only lines, within a token budget, and the decision about what was shown.

    Blocks inside the changeable scope are not repeated (they are listed in full under CHANGEABLE BLOCKS); a
    short marker keeps the order readable. When everything does not fit: long blocks are clipped first; then
    only the headings and an even spread of the other blocks are kept (the first and the last always), and
    every gap says how many blocks it hides. Nothing here is hidden from the person: the decision is stored
    with the proposal and shown in the coverage statement.
    """
    leaves = captured.leaves()
    total = len(leaves)
    if total == 0:
        return [], ContextDecision("none", 0, 0, 0, 0, budget_tokens)

    def kind_of(b: Any) -> str:
        return f"heading {b.level or 1}" if b.type == "heading" else b.type

    def line_for(b: Any, limit: int | None) -> str:
        label = reverse[b.id]
        if b.id in target.scope_ids:
            return f"({label} {kind_of(b)}) [changeable: shown in full above]"
        text = " ".join(b.text.split())
        if limit is not None and len(text) > limit:
            text = text[: limit - 1] + "…"
        return f"({label} {kind_of(b)}) {text}"

    def cost(lines: list[str]) -> int:
        return sum(measure(x) + 1 for x in lines)

    def shown_text_chars(lines: list[str]) -> int:
        return sum(len(x) for x in lines)

    full = [line_for(b, None) for b in leaves]
    if cost(full) <= budget_tokens:
        return full, ContextDecision(
            "full", total, total, shown_text_chars(full), cost(full), budget_tokens
        )
    clipped = [line_for(b, None if b.level else CLIP_CHARS) for b in leaves]
    n_clipped = sum(1 for a, b in zip(full, clipped, strict=True) if a != b)
    if cost(clipped) <= budget_tokens:
        return clipped, ContextDecision(
            "clipped",
            total,
            total,
            shown_text_chars(clipped),
            cost(clipped),
            budget_tokens,
            clipped_blocks=n_clipped,
        )
    # sampled: every heading, the first and the last block, then an even spread of the rest
    keep = {i for i, b in enumerate(leaves) if b.level} | {0, total - 1}
    used = sum(measure(clipped[i]) + 1 for i in keep)
    rest = [i for i in range(total) if i not in keep]
    if rest:
        step = max(
            1, math.ceil(len(rest) / max(1, int(len(rest) * budget_tokens / max(1, cost(clipped)))))
        )
        for offset in range(step):
            for i in rest[offset::step]:
                c = measure(clipped[i]) + 1
                if used + c > budget_tokens:
                    continue
                keep.add(i)
                used += c

    def assemble() -> list[str]:
        lines: list[str] = []
        gap = 0
        for i in range(total):
            if i in keep:
                if gap:
                    lines.append(f"… {gap} block{'s' if gap != 1 else ''} not shown …")
                    gap = 0
                lines.append(clipped[i])
            else:
                gap += 1
        if gap:
            lines.append(f"… {gap} block{'s' if gap != 1 else ''} not shown …")
        return lines

    lines = assemble()
    # the gap markers cost tokens too: give back the blocks added last until the whole thing fits
    # the budget is a hard cap: middle blocks go first, then the first and last block, then headings from the end
    ordered = sorted(keep, reverse=True)
    middle = [i for i in ordered if i not in {0, total - 1} and not leaves[i].level]
    ends = [i for i in ordered if i in {0, total - 1} and not leaves[i].level]
    heads = [i for i in ordered if leaves[i].level]
    droppable = [*reversed(heads), *reversed(ends), *reversed(middle)]
    while cost(lines) > budget_tokens and droppable:
        keep.discard(droppable.pop())
        lines = assemble()
    shown = len(keep)
    return lines, ContextDecision(
        "sampled",
        total,
        shown,
        shown_text_chars(lines),
        cost(lines),
        budget_tokens,
        clipped_blocks=sum(1 for i in keep if full[i] != clipped[i]),
        omitted_blocks=total - shown,
    )


def _clip(text: str, limit: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"
