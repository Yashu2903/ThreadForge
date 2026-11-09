"""Positions inside a block's text: UTF-16 code units, and the exact mapping to the document.

A RANGE SELECTION is `(block_id, start, end)`, counted in UTF-16 code units of the block's TEXT, because
that is what the browser (JavaScript strings) and the collaboration service (Yjs, which also counts UTF-16
units) use. The same text is built the same way everywhere (the TypeScript twin is `blockUnits` in
packages/editor-schema/src/units.ts and both are tested against `test/fixtures/range-cases.json`).

THE MAPPING. The text of a text-holding block (paragraph, heading, diagram) is its inline children in
order:
  * a text node contributes its characters, counted in UTF-16 units (a character outside the Basic
    Multilingual Plane, such as an emoji, is TWO units: a surrogate pair);
  * a hard break contributes one unit, the line feed "\\n";
  * an inline image contributes one unit, U+FFFC (the object replacement character).
Marks (bold, link, ...) take no space. Block-level wrappers (a list, a table, a quote) have no text of
their own: they are containers, and a range must name the paragraph inside them.

SURROGATE PAIRS. A boundary must not fall between the two halves of a pair; such a range is refused
(`range_invalid`) rather than rounded, because rounding would silently change what the person selected.
"""

from __future__ import annotations

import hashlib
from typing import Any

OBJECT_REPLACEMENT = "￼"
TEXT_HOLDERS = ("paragraph", "heading", "diagram")


class RangeError(ValueError):
    pass


def holds_text(node: dict[str, Any]) -> bool:
    return node.get("type") in TEXT_HOLDERS


def block_units(node: dict[str, Any]) -> str:
    """The unit text of a text-holding block. Raises RangeError for a container."""
    if not holds_text(node):
        raise RangeError("not a text block")
    parts: list[str] = []
    for child in node.get("content") or []:
        kind = child.get("type")
        if kind == "text":
            parts.append(str(child.get("text", "")))
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "image":
            parts.append(OBJECT_REPLACEMENT)
        else:
            raise RangeError(f"unexpected inline node {kind!r}")
    return "".join(parts)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text: str, start: int, end: int) -> str:
    """text[start:end] in UTF-16 units. RangeError for a bad range or one that splits a surrogate pair."""
    units = text.encode("utf-16-le")
    total = len(units) // 2
    if not (0 <= start <= end <= total):
        raise RangeError("range outside the text")
    for boundary in (start, end):
        if 0 < boundary < total:
            unit = int.from_bytes(units[boundary * 2 : boundary * 2 + 2], "little")
            before = int.from_bytes(units[boundary * 2 - 2 : boundary * 2], "little")
            if 0xDC00 <= unit <= 0xDFFF and 0xD800 <= before <= 0xDBFF:
                raise RangeError("range splits a surrogate pair")
    return units[start * 2 : end * 2].decode("utf-16-le")


def text_hash(text: str) -> str:
    """SHA-256 (hex) of the UTF-8 bytes of the selected text: what the client computes at send time."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
