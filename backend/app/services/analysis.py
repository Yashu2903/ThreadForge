"""Analysis from the API's side: ask for it, cancel it, look at it, browse the passages.

The API never analyzes anything and never calls a provider (that is the AI worker's job). It only
decides whether an analysis is ALLOWED, writes the job row, and reads results back.

ADMISSION (`request_analysis`), all in one transaction that starts by locking the room row and
re-reading the caller's role under that lock:
  1. the version belongs to this room, is not removed, and its source is active;
  2. the AI gate (app/ai/access.py): role, configuration, global switch, capability status, room
     switch, can_write on the anchor document, consent;
  3. the keys: the embedding key is always needed; the generation key too for files that might need
     the vision model (PDF, DOCX). The choice (server or personal key) is made NOW and written on
     the job, and the worker never switches to the other one;
  4. the size cap (`AI_MAX_NEW_ANALYSIS_PAGES`): refused before anything is queued;
  5. `jobs.enqueue`: a ready index is reused (no job, no provider call), a queued/running job is
     shared, otherwise a new job is created.

The first requester's key pays for a shared job. That is stated in docs/ai.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import access
from app.ai.analysis import jobs
from app.ai.estimate import AnalysisEstimate, estimate_analysis
from app.config import Settings
from app.errors import (
    analysis_cap_exceeded,
    insufficient_role,
    key_rejected,
    key_unavailable,
    source_not_found,
    source_version_not_found,
)
from app.models import Source, SourceVersion
from app.services.rooms import lock_room_and_actor

_SRC = Source.__table__
_VER = SourceVersion.__table__
EDIT_ROLES = ("owner", "editor")
MANUAL_NAMESPACE = uuid.UUID("5d0c1a0e-9a5c-4f43-8d8e-0f6c4b7d1a11")


def manual_waiter_id(user_id: uuid.UUID, version_id: uuid.UUID) -> uuid.UUID:
    """The same person pressing "Analyze now" twice is the same waiter."""
    return uuid.uuid5(MANUAL_NAMESPACE, f"{user_id}:{version_id}")


@dataclass(frozen=True)
class AnalysisAdmission:
    status: str  # ready | created | joined
    job_id: uuid.UUID | None
    generation_id: uuid.UUID | None
    key_source_generation: str
    key_source_embedding: str
    estimate: AnalysisEstimate


async def request_analysis(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    anchor_document_id: uuid.UUID | None,
) -> AnalysisAdmission:
    async with sessionmaker() as db, db.begin():
        _locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        ver = (
            await db.execute(
                select(_VER)
                .join(_SRC, (_SRC.c.id == _VER.c.source_id) & (_SRC.c.room_id == _VER.c.room_id))
                .where(
                    _VER.c.id == version_id,
                    _VER.c.source_id == source_id,
                    _VER.c.room_id == room_id,
                    _VER.c.analysis_state != "removed",
                    _SRC.c.state == "active",
                )
            )
        ).one_or_none()
        if ver is None:
            raise source_not_found()
        admission = await access.gate(
            db,
            settings,
            user_id=actor_id,
            role=role,
            room_id=room_id,
            document_id=anchor_document_id,
        )
        gen = admission.key_sources["generation"]
        emb = admission.key_sources["embedding"]
        needs_vision = ver.kind in ("pdf", "docx")
        if emb.source is None:
            raise _key_error(emb.problem, "google")
        if needs_vision and gen.source is None:
            raise _key_error(gen.problem, "openai")
        if ver.page_count is not None and ver.page_count > settings.ai_max_new_analysis_pages:
            raise analysis_cap_exceeded(
                "max_new_analysis_pages", settings.ai_max_new_analysis_pages, ver.page_count
            )
        gen_source = gen.source or emb.source
        assert anchor_document_id is not None  # noqa: S101 - the gate refused otherwise
        queued = await jobs.enqueue(
            db,
            settings,
            room_id=room_id,
            version_id=version_id,
            requested_by=actor_id,
            anchor_document_id=anchor_document_id,
            key_source_generation=gen_source,
            key_source_embedding=emb.source,
            waiter_id=manual_waiter_id(actor_id, version_id),
            waiter_kind="manual",
        )
        estimate = estimate_analysis(
            settings,
            text_chars=ver.text_chars,
            likely_scanned_pages=ver.likely_scanned_pages,
            image_count=ver.image_count,
            kind=ver.kind,
        )
    return AnalysisAdmission(
        queued.status, queued.job_id, queued.generation_id, gen_source, emb.source, estimate
    )


def _key_error(problem: str | None, provider: str):
    return key_rejected(provider) if problem == "key_rejected" else key_unavailable(provider)


async def cancel_manual(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    version_id: uuid.UUID,
) -> int:
    """The caller no longer wants this file analyzed. Cancels the job only if nobody else waits."""
    async with sessionmaker() as db, db.begin():
        _locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        exists = (
            await db.execute(
                select(_VER.c.id).where(_VER.c.id == version_id, _VER.c.room_id == room_id)
            )
        ).first()
        if exists is None:
            raise source_version_not_found()
    return await jobs.release_waiter(sessionmaker, manual_waiter_id(actor_id, version_id))


# ---- reading --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisView:
    version_id: uuid.UUID
    analysis_state: str
    error_code: str | None
    job_state: str | None
    job_stage: str | None
    job_error_code: str | None
    attempts: int | None
    waiting: int
    generation_id: uuid.UUID | None
    chunk_count: int | None
    figure_count: int | None
    figures_found: int | None
    pages_total: int | None
    pages_unreadable: int | None
    warnings: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    embed_model: str | None
    published_at: datetime | None


async def get_analysis(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, version_id: uuid.UUID
) -> AnalysisView:
    async with sessionmaker() as db:
        ver = (
            await db.execute(
                select(
                    _VER.c.id,
                    _VER.c.analysis_state,
                    _VER.c.error_code,
                    _VER.c.current_generation_id,
                )
                .join(_SRC, (_SRC.c.id == _VER.c.source_id) & (_SRC.c.room_id == _VER.c.room_id))
                .where(
                    _VER.c.id == version_id,
                    _VER.c.room_id == room_id,
                    _VER.c.analysis_state != "removed",
                    _SRC.c.state == "active",
                )
            )
        ).one_or_none()
        if ver is None:
            raise source_version_not_found()
        job = (
            await db.execute(
                text(
                    """SELECT j.id, j.state, j.stage, j.error_code, j.attempts,
                              (SELECT count(*) FROM analysis_job_waiters w WHERE w.job_id = j.id) AS waiting
                         FROM analysis_jobs j WHERE j.source_version_id = :v
                        ORDER BY j.created_at DESC LIMIT 1"""
                ),
                {"v": version_id},
            )
        ).one_or_none()
        gen = None
        if ver.current_generation_id is not None:
            gen = (
                await db.execute(
                    text(
                        """SELECT id, chunk_count, figure_count, figures_found, pages_total,
                                  pages_unreadable, warnings, gaps, embed_model, published_at
                             FROM index_generations WHERE id = :g AND state = 'ready'"""
                    ),
                    {"g": ver.current_generation_id},
                )
            ).one_or_none()
    return AnalysisView(
        version_id=version_id,
        analysis_state=ver.analysis_state,
        error_code=ver.error_code,
        job_state=job.state if job else None,
        job_stage=job.stage if job else None,
        job_error_code=job.error_code if job else None,
        attempts=job.attempts if job else None,
        waiting=job.waiting if job and job.state in ("queued", "running") else 0,
        generation_id=gen.id if gen else None,
        chunk_count=gen.chunk_count if gen else None,
        figure_count=gen.figure_count if gen else None,
        figures_found=gen.figures_found if gen else None,
        pages_total=gen.pages_total if gen else None,
        pages_unreadable=gen.pages_unreadable if gen else None,
        warnings=list(gen.warnings) if gen else [],
        gaps=list(gen.gaps) if gen else [],
        embed_model=gen.embed_model if gen else None,
        published_at=gen.published_at if gen else None,
    )


@dataclass(frozen=True)
class PassageView:
    id: uuid.UUID
    ordinal: int
    kind: str
    text: str
    extraction: str
    ocr_confidence: float | None
    locator: dict[str, Any]
    flags: dict[str, Any]
    section_key: str | None
    token_count: int
    figure_number: int | None


async def list_passages(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    after: int,
    limit: int,
    query: str | None,
) -> tuple[list[PassageView], int | None]:
    """The CURRENT ready generation's passages of one version, in reading order, optionally narrowed
    by a full-text query (the lexical half of retrieval). Returns (rows, next `after` or None).
    Filters by room and by the version's current generation inside the SQL."""
    async with sessionmaker() as db:
        live = (
            await db.execute(
                select(_VER.c.id)
                .join(_SRC, (_SRC.c.id == _VER.c.source_id) & (_SRC.c.room_id == _VER.c.room_id))
                .where(
                    _VER.c.id == version_id,
                    _VER.c.room_id == room_id,
                    _VER.c.analysis_state != "removed",
                    _SRC.c.state == "active",
                )
            )
        ).first()
        if live is None:
            raise source_version_not_found()
        rows = (
            await db.execute(
                text(
                    """SELECT c.id, c.ordinal, c.kind, c.text, c.extraction, c.ocr_confidence,
                              c.locator, c.flags, c.section_key, c.token_count, f.figure_number
                         FROM source_versions v
                         JOIN index_generations g ON g.id = v.current_generation_id AND g.state = 'ready'
                         JOIN chunks c ON c.generation_id = g.id AND c.room_id = :room
                         LEFT JOIN figures f ON f.chunk_id = c.id
                        WHERE v.id = :ver AND v.room_id = :room AND v.analysis_state = 'ready'
                          AND c.ordinal > :after
                          AND (CAST(:q AS text) IS NULL
                               OR c.tsv @@ websearch_to_tsquery('english', CAST(:q AS text)))
                        ORDER BY c.ordinal LIMIT :lim"""
                ),
                {"room": room_id, "ver": version_id, "after": after, "q": query, "lim": limit + 1},
            )
        ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    views = [
        PassageView(
            r.id,
            r.ordinal,
            r.kind,
            r.text,
            r.extraction,
            r.ocr_confidence,
            r.locator,
            r.flags,
            r.section_key,
            r.token_count,
            r.figure_number,
        )
        for r in rows
    ]
    return views, (rows[-1].ordinal if more and rows else None)
