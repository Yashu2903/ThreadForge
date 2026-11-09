"""Chunking policy `chunk-v1`: turn the extractor's reading-order units into passages.

A CHUNK is the unit the search index stores and the answer cites: a few paragraphs of one file, with
a locator (page range, heading path) that a person can follow back to the original. Chunks are
what get embedded, so their size matters: too big and one vector blurs several ideas, too small and
there is no context. The rules (all pure, no I/O, so they can be tested exhaustively):

  * TARGET about `target_tokens` (default 400); never above `max_tokens` (a hard limit below the
    embedding model's input limit). Token counts are the conservative estimate of app/ai/pricing.py,
    so real chunks are somewhat smaller than the numbers suggest.
  * STRUCTURE-AWARE. A heading always starts a new chunk and is the first line of it; the heading
    path (Section > Subsection) is recorded in the locator. A page boundary closes the chunk when
    it is already at least half the target. Paragraphs are never cut mid-sentence unless one
    sentence alone exceeds the hard maximum (then it is cut at a word boundary).
  * TABLES stay readable: one line per row, cells joined by " | ", and the HEADER ROW is repeated at
    the top of every chunk the table needs.
  * OVERLAP: when prose continues inside the same section, the next chunk starts with the last
    `overlap_tokens` of the previous one, so a sentence at a boundary is findable from both sides.
  * A chunk's `extraction` is the least trustworthy way any of its text was obtained
    (vision_ocr < tesseract < text_layer/docx/plain); its OCR confidence is the lowest of its parts.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.ai.pricing import estimate_text_tokens
from app.config import Settings

CHUNK_POLICY = "chunk-v1"
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_RANK = {"vision_ocr": 0, "tesseract": 1, "text_layer": 2, "docx": 3, "plain": 3}


@dataclass(frozen=True)
class Unit:
    """One piece of the source in reading order."""

    t: str  # heading | para | list | table
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()
    page: int | None = None
    extraction: str = "plain"  # text_layer | tesseract | vision_ocr | docx | plain
    ocr_confidence: float | None = None
    heading_path: tuple[str, ...] | None = None  # given for PDF pages (from the outline)
    low_agreement: bool = False


@dataclass
class DraftChunk:
    text: str
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]
    extraction: str
    ocr_confidence: float | None
    low_agreement: bool
    token_count: int
    section_key: str = ""
    ordinal: int = 0

    @property
    def text_hash(self) -> bytes:
        return hashlib.sha256(self.text.encode("utf-8")).digest()

    @property
    def locator(self) -> dict:
        loc: dict = {"heading_path": list(self.heading_path)}
        if self.page_start is not None:
            loc["page_start"] = self.page_start
            loc["page_end"] = self.page_end
        return loc


@dataclass
class _Acc:
    parts: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    extraction: str = "docx"
    conf: float | None = None
    low_agreement: bool = False
    path: tuple[str, ...] = ()
    started_with_heading: bool = False
    has_body: bool = False


def tokens(settings: Settings, text: str) -> int:
    return max(1, estimate_text_tokens(settings, text))


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text) if s.strip()]


def hard_split(settings: Settings, text: str, max_tokens: int) -> list[str]:
    """Cut text that is too long even as one sentence, at word boundaries, under max_tokens."""
    out: list[str] = []
    current: list[str] = []
    for word in text.split():
        trial = " ".join([*current, word])
        if current and tokens(settings, trial) > max_tokens:
            out.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        out.append(" ".join(current))
    return out


def _pieces(settings: Settings, text: str, max_tokens: int) -> list[str]:
    """A paragraph as pieces that each fit under max_tokens, cut at sentences first."""
    if tokens(settings, text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    for sentence in split_sentences(text) or [text]:
        if tokens(settings, sentence) <= max_tokens:
            pieces.append(sentence)
        else:
            pieces.extend(hard_split(settings, sentence, max_tokens))
    return pieces


def table_lines(rows: tuple[tuple[str, ...], ...]) -> list[str]:
    return [" | ".join(cell.strip() for cell in row) for row in rows if any(c.strip() for c in row)]


def chunk_units(settings: Settings, units: list[Unit]) -> list[DraftChunk]:
    target = settings.ai_chunk_target_tokens
    hard = settings.ai_chunk_max_tokens
    overlap_chars = int(settings.ai_chunk_overlap_tokens * settings.ai_chars_per_token / 1.1)
    # room left for a piece once a heading line and the carried overlap are in the chunk too
    piece_max = max(20, hard - settings.ai_chunk_overlap_tokens - 90)
    chunks: list[DraftChunk] = []
    acc = _Acc()
    path: list[tuple[int, str]] = []  # the heading stack: (level, title)

    def current_path() -> tuple[str, ...]:
        return tuple(title for _level, title in path)

    def acc_tokens() -> int:
        return tokens(settings, "\n".join(acc.parts)) if acc.parts else 0

    def flush(*, keep_overlap: bool) -> None:
        nonlocal acc
        if not acc.has_body:
            acc = _Acc(path=current_path())
            return
        text = "\n".join(acc.parts).strip()
        if text:
            pages = acc.pages
            chunks.append(
                DraftChunk(
                    text=text,
                    page_start=min(pages) if pages else None,
                    page_end=max(pages) if pages else None,
                    heading_path=acc.path,
                    extraction=acc.extraction,
                    ocr_confidence=acc.conf,
                    low_agreement=acc.low_agreement,
                    token_count=tokens(settings, text),
                )
            )
        tail = ""
        if keep_overlap and overlap_chars > 0 and text:
            cut = text[-overlap_chars:]
            tail = cut[cut.find(" ") + 1 :] if " " in cut else cut
        acc = _Acc(path=current_path())
        if tail.strip():
            acc.parts.append(tail.strip())

    def add(piece: str, unit: Unit) -> None:
        if not acc.parts and not acc.has_body:
            acc.path = current_path()
        if unit.page is not None and (not acc.pages or acc.pages[-1] != unit.page):
            acc.pages.append(unit.page)
        rank = _RANK.get(unit.extraction, 3)
        if not acc.has_body or rank < _RANK.get(acc.extraction, 3):
            acc.extraction = unit.extraction
        if unit.ocr_confidence is not None:
            acc.conf = (
                unit.ocr_confidence if acc.conf is None else min(acc.conf, unit.ocr_confidence)
            )
        acc.low_agreement = acc.low_agreement or unit.low_agreement
        acc.parts.append(piece)
        acc.has_body = True

    def fits(piece: str) -> bool:
        return tokens(settings, "\n".join([*acc.parts, piece])) <= target

    last_page: int | None = None
    for unit in units:
        if unit.heading_path is not None:  # PDF pages: the outline decides the heading path
            new_path = tuple(unit.heading_path)
            if new_path != current_path():
                flush(keep_overlap=False)
                path[:] = [(i, t) for i, t in enumerate(new_path)]
                acc.path = new_path
        page_changed = unit.page is not None and last_page is not None and unit.page != last_page
        if page_changed and acc.has_body and acc_tokens() >= target // 2:
            flush(keep_overlap=True)
        if unit.page is not None:
            last_page = unit.page

        if unit.t == "heading":
            flush(keep_overlap=False)
            while path and path[-1][0] >= unit.level:
                path.pop()
            path.append((unit.level, unit.text.strip()))
            acc = _Acc(path=current_path(), started_with_heading=True)
            acc.parts.append(unit.text.strip()[:200])
            if unit.page is not None:
                acc.pages.append(unit.page)
            continue

        if unit.t == "table":
            lines = table_lines(unit.rows)
            if not lines:
                continue
            flush(keep_overlap=False)
            header, body = lines[0], lines[1:]
            current_rows: list[str] = []
            for row in body or [""]:
                trial = "\n".join([header, *current_rows, row] if row else [header])
                if current_rows and tokens(settings, trial) > target:
                    add("\n".join([header, *current_rows]), unit)
                    flush(keep_overlap=False)
                    current_rows = []
                for part in _pieces(settings, row, max(1, piece_max - tokens(settings, header))):
                    if part:
                        current_rows.append(part)
            add("\n".join([header, *current_rows]), unit)
            flush(keep_overlap=False)
            continue

        text = unit.text.strip()
        if not text:
            continue
        if unit.t == "list":
            text = "- " + text
        for piece in _pieces(settings, text, piece_max):
            if acc.has_body and not fits(piece):
                flush(keep_overlap=True)
            add(piece, unit)
    flush(keep_overlap=False)

    for index, chunk in enumerate(chunks):
        chunk.ordinal = index
    assign_sections(chunks)
    return chunks


def assign_sections(chunks: list[DraftChunk]) -> None:
    """`section_key` groups chunks for coverage summaries: the heading path when there is one, else
    blocks of ten pages, else blocks of twenty chunks."""
    for chunk in chunks:
        if chunk.heading_path:
            chunk.section_key = " > ".join(chunk.heading_path)[:300]
        elif chunk.page_start is not None:
            block = (chunk.page_start - 1) // 10
            chunk.section_key = f"pages {block * 10 + 1}-{block * 10 + 10}"
        else:
            chunk.section_key = f"part {chunk.ordinal // 20 + 1}"
