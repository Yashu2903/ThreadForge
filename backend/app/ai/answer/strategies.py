"""How a request's evidence is gathered, per task kind. Everything the model will see is decided here, by
the server, from exactly the sources the request pinned and the context it explicitly enabled.

    qa              embed the query, hybrid retrieval over the pinned generations, one answer call.
    compare         the same retrieval run PER SOURCE with an equal share of the evidence budget, so one
                    long file cannot crowd out the other; then one comparison call.
    summarize       NOT retrieval. Each source is cut into its sections (heading path, else page ranges),
                    sections are summarized in batches (map calls), and the answer call combines the
                    summaries (reduce). Above the per-source budget the sections are SAMPLED evenly (first
                    and last always kept) and the coverage statement says so.
    document_review the captured document as labelled blocks; too big for the budget -> sections by
                    heading, map then reduce. Pinned sources, if any, add retrieved passages.

THE COVERAGE STATEMENT is computed here from counts, never written by the model: per source the sections
read of the total and the mode (`full`, `sampled` or `retrieval`), figures examined versus found, the
document's blocks included versus total, chat messages included, and a note for anything left out. Above
the caps the strategies sample and say so; nothing implies coverage that did not happen.

SCOPE. All database reads here are restricted by `room_id` and by the request's FROZEN generation ids
inside the query. Chat messages and document blocks are only those the request enabled; sources-only
requests contain no document text, no chat and no earlier AI output.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.analysis.chunking import tokens
from app.ai.analysis.gateway import AnalysisGateway
from app.ai.answer import capture as cap
from app.ai.answer import prompts
from app.ai.answer.answer import EvidenceItem, EvidenceSet
from app.ai.retrieval import Passage, retrieve
from app.config import Settings
from app.services import messages
from app.storage import Storage

FIXED_OVERHEAD_TOKENS = 3500  # system prompt, schema, instruction
CHAT_MESSAGE_CHARS = 1500
DOC_BLOCK_CHARS = 6000
MAP_OUTPUT_TOKENS = 3000


@dataclass
class SourceInfo:
    version_id: uuid.UUID
    source_id: uuid.UUID
    name: str
    version_no: int
    kind: str
    generation_id: uuid.UUID
    figure_count: int
    chunk_count: int


@dataclass
class ImageToSend:
    label: str
    data: bytes
    media_type: str
    sha256: str
    page: int | None
    figure_number: int | None
    source_version_id: uuid.UUID | None
    chunk_id: uuid.UUID | None


@dataclass
class Gathered:
    evidence: EvidenceSet = field(default_factory=EvidenceSet)
    images: list[ImageToSend] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    empty: bool = False  # nothing relevant was found and there is no other context: no model call


# ---- reading the pinned sources --------------------------------------------------------------------------


async def load_sources(
    db: AsyncSession, room_id: uuid.UUID, request_id: uuid.UUID
) -> list[SourceInfo]:
    rows = (
        await db.execute(
            text(
                """SELECT v.id AS vid, v.source_id, s.display_name, v.version_no, v.kind, rs.generation_id,
                          g.figure_count, g.chunk_count, v.analysis_state
                     FROM ai_request_sources rs
                     JOIN source_versions v ON v.id = rs.source_version_id AND v.room_id = rs.room_id
                     JOIN sources s ON s.id = v.source_id AND s.room_id = v.room_id
                     JOIN index_generations g ON g.id = rs.generation_id AND g.room_id = rs.room_id
                    WHERE rs.request_id = :r AND rs.room_id = :room AND v.analysis_state <> 'removed'
                      AND s.state = 'active'
                    ORDER BY rs.position"""
            ),
            {"r": request_id, "room": room_id},
        )
    ).all()
    return [
        SourceInfo(
            r.vid,
            r.source_id,
            r.display_name,
            r.version_no,
            r.kind,
            r.generation_id,
            r.figure_count,
            r.chunk_count,
        )
        for r in rows
    ]


def passage_item(ev: EvidenceSet, p: Passage, names: dict[uuid.UUID, str]) -> EvidenceItem:
    loc = {
        k: v
        for k, v in (p.locator or {}).items()
        if k in ("heading_path", "page_start", "page_end", "figure_number")
    }
    return ev.add(
        "P",
        kind="passage",
        text=p.text,
        extraction=p.extraction,
        source_version_id=p.source_version_id,
        source_name=names.get(p.source_version_id, ""),
        locator=loc,
        chunk_ids=[p.id],
        figure_chunk_id=p.id if p.kind == "figure" else None,
        low_agreement=bool((p.flags or {}).get("low_agreement")),
        is_figure=p.kind == "figure",
    )


def read_figure(storage: Storage, p: Passage, label: str) -> ImageToSend | None:
    """The stored (re-encoded) image of a figure passage, or None when the file is gone."""
    if not p.figure_storage_key:
        return None
    try:
        data = storage.path_for("figures", p.figure_storage_key).read_bytes()
    except (OSError, ValueError):
        return None
    return ImageToSend(
        label=label,
        data=data,
        media_type=p.figure_media_type or "image/png",
        sha256=hashlib.sha256(data).hexdigest(),
        page=p.page_start,
        figure_number=p.figure_number,
        source_version_id=p.source_version_id,
        chunk_id=p.id,
    )


def passage_record(label: str, p: Passage) -> dict[str, Any]:
    return {
        "label": label,
        "chunk_id": str(p.id),
        "source_version_id": str(p.source_version_id),
        "generation_id": str(p.generation_id),
        "ordinal": p.ordinal,
        "kind": p.kind,
        "locator": p.locator,
        "extraction": p.extraction,
        "text": p.text,
        "lexical_rank": p.lexical_rank,
        "vector_rank": p.vector_rank,
        "score": p.score,
    }


# ---- budgets ---------------------------------------------------------------------------------------------------


def budgets(settings: Settings, *, has_doc: bool, has_chat: bool) -> dict[str, int]:
    room = settings.ai_max_input_tokens - FIXED_OVERHEAD_TOKENS
    doc = int(room * 0.35) if has_doc else 0
    chat = int(room * 0.10) if has_chat else 0
    evidence = max(1000, min(settings.ai_retrieval_evidence_tokens, room - doc - chat))
    return {"document": doc, "chat": chat, "evidence": evidence}


# ---- chat and document context ---------------------------------------------------------------------------------


async def chat_items(
    db: AsyncSession,
    room_id: uuid.UUID,
    ids: list[str],
    ev: EvidenceSet,
    budget: int,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Exact copies of the explicitly included, still-live messages of THIS room, oldest first. A
    message deleted since is excluded (and the manifest says so)."""
    if not ids:
        return [], []
    rows = await messages.chat_copies(db, room_id, ids)
    records: list[dict[str, Any]] = []
    exclusions: list[str] = []
    used = 0
    found = {str(r.id) for r in rows}
    exclusions += [f"chat_message_missing:{i}" for i in ids if i not in found]
    for r in rows:
        if r.deleted or r.body is None:
            exclusions.append(f"chat_message_deleted:{r.id}")
            continue
        body = r.body[:CHAT_MESSAGE_CHARS]
        line = f"{r.author_name or 'Someone'}: {body}"
        cost = tokens(settings, line)
        if used + cost > budget:
            exclusions.append(f"chat_message_over_budget:{r.id}")
            continue
        used += cost
        ev.add("C", kind="chat", text=line, extraction="plain")
        records.append(
            {
                "id": str(r.id),
                "author": r.author_name,
                "created_at": r.created_at.isoformat(),
                "body": r.body,
            }
        )
    return records, exclusions


def doc_items(
    captured: cap.Captured,
    blocks: list[cap.DocBlock],
    ev: EvidenceSet,
    budget: int,
    settings: Settings,
) -> tuple[int, bool]:
    """Label document blocks D1.. within the budget. Returns (blocks included, all of them fit)."""
    used = 0
    included = 0
    for b in blocks:
        body = b.text[:DOC_BLOCK_CHARS]
        prefix = ("#" * b.level + " ") if b.level else ""
        cost = tokens(settings, prefix + body)
        if used + cost > budget:
            return included, False
        used += cost
        included += 1
        ev.add(
            "D",
            kind="document",
            text=prefix + body,
            extraction="plain",
            source_name="the open document",
            locator={"block_id": b.id, "block_type": b.type},
        )
    return included, True


# ---- section summaries (summarize, large documents) ---------------------------------------------------------------------


@dataclass
class Section:
    key: str
    text: str
    tokens: int
    chunk_ids: list[uuid.UUID]
    page_start: int | None
    page_end: int | None
    extraction: str
    version_id: uuid.UUID | None
    source_name: str
    low_agreement: bool = False
    locator: dict[str, Any] = field(default_factory=dict)


_RANK = {"vision_ocr": 0, "vision": 0, "tesseract": 1, "text_layer": 2, "docx": 3, "plain": 3}


async def load_sections(db: AsyncSession, room_id: uuid.UUID, src: SourceInfo) -> list[Section]:
    rows = (
        await db.execute(
            text(
                """SELECT c.id, c.ordinal, c.section_key, c.text, c.token_count, c.extraction, c.page_start,
                          c.page_end, c.flags, c.locator
                     FROM chunks c
                    WHERE c.room_id = :room AND c.generation_id = :gen
                    ORDER BY c.ordinal"""
            ),
            {"room": room_id, "gen": src.generation_id},
        )
    ).all()
    sections: list[Section] = []
    for r in rows:
        key = r.section_key or "part"
        if sections and sections[-1].key == key:
            s = sections[-1]
            s.text += "\n\n" + r.text
            s.tokens += r.token_count
            s.chunk_ids.append(r.id)
            if r.page_start is not None:
                s.page_start = (
                    r.page_start if s.page_start is None else min(s.page_start, r.page_start)
                )
                s.page_end = max(s.page_end or 0, r.page_end or r.page_start)
            if _RANK.get(r.extraction, 3) < _RANK.get(s.extraction, 3):
                s.extraction = r.extraction
            s.low_agreement = s.low_agreement or bool((r.flags or {}).get("low_agreement"))
        else:
            sections.append(
                Section(
                    key=key,
                    text=r.text,
                    tokens=r.token_count,
                    chunk_ids=[r.id],
                    page_start=r.page_start,
                    page_end=r.page_end or r.page_start,
                    extraction=r.extraction,
                    version_id=src.version_id,
                    source_name=src.name,
                    low_agreement=bool((r.flags or {}).get("low_agreement")),
                    locator={
                        "section_key": key,
                        "heading_path": (r.locator or {}).get("heading_path", []),
                    },
                )
            )
    return sections


def sample(sections: list[Section], budget: int) -> tuple[list[Section], str]:
    """All sections when they fit; otherwise an even spread, always keeping the first and the last."""
    total = sum(s.tokens for s in sections)
    if total <= budget or len(sections) <= 2:
        return sections, "full"
    keep = {0, len(sections) - 1}
    used = sections[0].tokens + sections[-1].tokens
    remaining = [i for i in range(1, len(sections) - 1)]
    # an even stride over the middle, then fill by stride offsets until the budget is spent
    step = max(
        1, len(remaining) // max(1, math.floor((budget - used) / max(1, total / len(sections))))
    )
    for offset in range(step):
        for i in remaining[offset::step]:
            if used + sections[i].tokens > budget:
                continue
            keep.add(i)
            used += sections[i].tokens
    return [s for i, s in enumerate(sections) if i in keep], "sampled"


def batches_of(sections: list[Section], batch_tokens: int) -> list[list[Section]]:
    out: list[list[Section]] = []
    cur: list[Section] = []
    used = 0
    for s in sections:
        if cur and used + s.tokens > batch_tokens:
            out.append(cur)
            cur, used = [], 0
        cur.append(s)
        used += s.tokens
    if cur:
        out.append(cur)
    return out


async def summarize_sections(
    gateway: AnalysisGateway,
    settings: Settings,
    sections: list[Section],
    ev: EvidenceSet,
    *,
    max_calls: int,
    doc_mode: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Map step: summarize sections in batches (calls capped by `max_calls`, sampling batches evenly if
    needed), then register each summary as labelled S evidence. Returns (records for the manifest, mode)."""
    mode = "full"
    batches = batches_of(sections, settings.ai_summarize_section_tokens)
    if len(batches) > max_calls:
        stride = len(batches) / max_calls
        batches = [batches[int(i * stride)] for i in range(max_calls)]
        mode = "sampled"
    records: list[dict[str, Any]] = []
    for batch in batches:
        refs: dict[str, Section] = {}
        temp = EvidenceSet()
        for sec in batch:
            body = sec.text
            if sec.tokens > settings.ai_summarize_section_tokens:
                body = body[
                    : int(settings.ai_summarize_section_tokens * settings.ai_chars_per_token / 1.1)
                ]
            item = temp.add("S", kind="section", text=body, extraction=sec.extraction)
            refs[item.label] = sec
        data = await gateway.structured(
            "map",
            prompts.map_parts(list(temp.items.values())),
            system=prompts.map_system(),
            schema=prompts.MAP_SCHEMA,
            schema_name=prompts.MAP_SCHEMA_NAME,
            max_output_tokens=min(MAP_OUTPUT_TOKENS, settings.ai_max_output_tokens),
        )
        got: dict[str, str] = {}
        if isinstance(data, dict) and isinstance(data.get("sections"), list):
            for row in data["sections"]:
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("ref"), str)
                    and isinstance(row.get("summary"), str)
                ):
                    got[row["ref"].strip()] = row["summary"].strip()
        for label, sec in refs.items():
            summary = got.get(label)
            fallback = not summary
            if fallback:
                summary = sec.text[:600]  # the model skipped it: its own opening, marked as such
            loc = dict(sec.locator)
            if sec.page_start is not None:
                loc["page_start"], loc["page_end"] = sec.page_start, sec.page_end
            item = ev.add(
                "S",
                kind="section",
                text=summary[:4000],
                extraction=sec.extraction,
                source_version_id=sec.version_id,
                source_name=sec.source_name,
                locator=loc,
                chunk_ids=list(sec.chunk_ids),
                low_agreement=sec.low_agreement,
            )
            records.append(
                {
                    "label": item.label,
                    "source_version_id": str(sec.version_id) if sec.version_id else None,
                    "section_key": sec.key,
                    "chunk_ids": [str(c) for c in sec.chunk_ids],
                    "summary": item.text,
                    "fallback": fallback,
                    "tokens": sec.tokens,
                }
            )
    return records, mode


# ---- the strategies ---------------------------------------------------------------------------------------------------------


def per_source_coverage(src: SourceInfo, **extra: Any) -> dict[str, Any]:
    return {
        "source_version_id": str(src.version_id),
        "source_name": src.name,
        "version_no": src.version_no,
        **extra,
    }


async def gather_retrieval(
    db: AsyncSession,
    gateway: AnalysisGateway,
    settings: Settings,
    storage: Storage,
    room_id: uuid.UUID,
    sources: list[SourceInfo],
    instruction: str,
    out: Gathered,
    budget: int,
    *,
    balanced: bool,
) -> None:
    """qa / compare: hybrid retrieval over the frozen generations; `balanced` runs it once per source
    with an equal share of the budget (compare)."""
    names = {s.version_id: s.name for s in sources}
    vector = (await gateway.embed_query(instruction))[0]
    max_images = max(0, settings.ai_max_images)
    top_k = min(settings.ai_retrieval_figure_top_k, max_images)
    if balanced:
        runs = [([s.generation_id], budget // max(1, len(sources))) for s in sources]
    else:
        runs = [([s.generation_id for s in sources], budget)]
    chosen: list[Passage] = []
    for gens, share in runs:
        res = await retrieve(
            db,
            settings,
            room_id=room_id,
            generation_ids=gens,
            query=instruction,
            query_vector=vector,
            evidence_tokens=share,
            figure_top_k=top_k,
        )
        chosen.extend(res.passages)
        out.manifest.setdefault("retrieval", []).append(
            {
                "generations": [str(g) for g in res.searched_generations],
                "considered": res.considered,
                "selected": len(res.passages),
                "dropped_for_budget": res.dropped_for_budget,
                "lexical_only": [str(g) for g in res.incompatible],
                "unavailable": [str(g) for g in res.unavailable],
            }
        )
    figure_ids = set()
    for p in chosen:
        if p.kind == "figure" and p.figure_storage_key and len(figure_ids) < top_k:
            figure_ids.add(p.id)
    sections_seen: dict[uuid.UUID, set[str]] = {}
    for p in chosen:
        item = passage_item(out.evidence, p, names)
        out.manifest.setdefault("passages", []).append(passage_record(item.label, p))
        if p.section_key:
            sections_seen.setdefault(p.source_version_id, set()).add(p.section_key)
        if p.id in figure_ids:
            image = read_figure(storage, p, item.label)
            if image is None:
                out.manifest.setdefault("exclusions", []).append(f"figure_file_missing:{p.id}")
            else:
                out.images.append(image)
    for s in sources:
        total_sections = await section_count(db, room_id, s.generation_id)
        used = [p for p in chosen if p.source_version_id == s.version_id]
        out.coverage.setdefault("sources", []).append(
            per_source_coverage(
                s,
                mode="retrieval",
                sections_total=total_sections,
                sections_read=len(sections_seen.get(s.version_id, set())),
                passages_used=len(used),
                figures_total=s.figure_count,
                figures_examined=len([p for p in used if p.kind == "figure"]),
            )
        )
    if not chosen:
        out.empty = True


async def section_count(db: AsyncSession, room_id: uuid.UUID, generation_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                text(
                    "SELECT count(DISTINCT coalesce(section_key, 'part')) FROM chunks "
                    "WHERE room_id = :room AND generation_id = :gen"
                ),
                {"room": room_id, "gen": generation_id},
            )
        ).scalar_one()
    )


async def gather_summary(
    db: AsyncSession,
    gateway: AnalysisGateway,
    settings: Settings,
    room_id: uuid.UUID,
    sources: list[SourceInfo],
    out: Gathered,
    *,
    max_calls: int,
) -> None:
    per_source_calls = max(1, max_calls // max(1, len(sources)))
    for src in sources:
        sections = await load_sections(db, room_id, src)
        chosen, mode = sample(sections, settings.ai_summarize_source_tokens)
        records, map_mode = await summarize_sections(
            gateway, settings, chosen, out.evidence, max_calls=per_source_calls
        )
        out.manifest.setdefault("sections", []).extend(records)
        read = len({r["section_key"] for r in records if not r["fallback"]})
        out.coverage.setdefault("sources", []).append(
            per_source_coverage(
                src,
                mode="sampled" if "sampled" in (mode, map_mode) else "full",
                sections_total=len(sections),
                sections_read=len(chosen) if map_mode == "full" else read,
                figures_total=src.figure_count,
                figures_examined=len([s for s in chosen if "Figure" in s.text[:12]]),
            )
        )
    if not out.evidence.items:
        out.empty = True


def document_sections(captured: cap.Captured) -> list[Section]:
    """The captured document as heading-delimited sections (a section starts at each heading)."""
    sections: list[Section] = []
    for b in captured.leaves():
        if b.level or not sections:
            title = b.text.strip()[:120] if b.level else "(start of document)"
            sections.append(
                Section(
                    key=title,
                    text="",
                    tokens=0,
                    chunk_ids=[],
                    page_start=None,
                    page_end=None,
                    extraction="plain",
                    version_id=None,
                    source_name="the open document",
                    locator={"document_section": title, "block_ids": []},
                )
            )
        s = sections[-1]
        s.text += b.text + "\n\n"
        s.tokens += max(1, len(b.text) // 3)
        s.locator["block_ids"].append(b.id)
    return sections
