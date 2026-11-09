"""Writing an index generation: build it invisibly, publish it in ONE transaction.

TERMS
  * generation   one complete index of one source version (its chunks, vectors and figures), built with
                 one configuration (embedding model and dimension, vision model, chunk policy).
  * building     while a worker writes chunks, the generation is `building`. Retrieval only ever
                 follows `source_versions.current_generation_id`, which still points at the previous
                 READY generation (or nothing), so partial work is invisible by construction.
  * publish      validate that the generation is complete, then in one transaction: mark it ready,
                 point the version at it, mark the old generation superseded, mark the job done. A
                 reader sees all of it or none of it.

Re-analysis never replaces a usable index with a partial one: until `publish` commits, the old
pointer stays; if the job dies, the building rows are deleted (here, or by `cleanup_ai`).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.analysis.chunking import CHUNK_POLICY, DraftChunk
from app.ai.analysis.jobs import PARSER_VERSION
from app.assets_pipeline import ImageRejected, process_image
from app.config import Settings
from app.logging_config import APP_LOGGER
from app.models import IndexGeneration, SourceVersion
from app.models.analysis import vector_literal
from app.services.room_lock import lock_room
from app.services.sources import emit_source_event
from app.storage import Storage

log = logging.getLogger(f"{APP_LOGGER}.ai.store")
_GEN = IndexGeneration.__table__
_VER = SourceVersion.__table__
FIGURE_NAMESPACE = "figures"
FIGURE_MAX_PIXELS = 40_000_000
INSERT_BATCH = 100


class IndexIncomplete(Exception):
    """The generation failed validation (counts, vectors, dimensions); it is not published."""


@dataclass
class StoredFigure:
    storage_key: str
    media_type: str
    width: int
    height: int
    byte_size: int
    sha256: bytes
    data: bytes  # the re-encoded image (kept in memory for the embedding / description step)


@dataclass
class ChunkRow:
    draft: DraftChunk
    kind: str = "text"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    figure_number: int | None = None
    figure: StoredFigure | None = None
    page: int | None = None
    locator_extra: dict = field(default_factory=dict)


async def start_generation(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    job: object,
    *,
    fingerprint: str,
    figure_mode: str,
) -> uuid.UUID:
    """A fresh `building` generation. Any `building` generation left by an earlier attempt of the SAME
    job (a crashed worker) is deleted first, so a crash and retry yields exactly one generation."""
    await discard_building(sessionmaker, storage, job.id)  # type: ignore[attr-defined]
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                text(
                    """INSERT INTO index_generations (room_id, source_version_id, job_id,
                           config_fingerprint, state, parser_version, chunk_policy, embed_model,
                           embed_dims, vision_model, figure_embedding_mode)
                       VALUES (:room, :ver, :job, :fp, 'building', :parser, :policy, :em, :ed, :vm, :fm)
                       RETURNING id"""
                ),
                {
                    "room": job.room_id,  # type: ignore[attr-defined]
                    "ver": job.source_version_id,  # type: ignore[attr-defined]
                    "job": job.id,  # type: ignore[attr-defined]
                    "fp": fingerprint,
                    "parser": PARSER_VERSION,
                    "policy": CHUNK_POLICY,
                    "em": settings.ai_embed_model,
                    "ed": settings.ai_embed_dimensions,
                    "vm": settings.ai_llm_model,
                    "fm": figure_mode,
                },
            )
        ).scalar_one()
    return row


def store_figure(
    storage: Storage, png_path: Path, *, max_pixels: int = FIGURE_MAX_PIXELS
) -> StoredFigure | None:
    """Re-encode a figure image through the existing image pipeline (EXIF/ICC/text chunks gone, only
    PNG/JPEG/WebP, pixel limits) and store the CLEAN result under an opaque key. None if the
    pipeline refuses the image."""
    try:
        processed = process_image(png_path, "image/png", max_pixels)
    except ImageRejected:
        return None
    data = processed.path.read_bytes()
    key = storage.new_key()
    storage.put(FIGURE_NAMESPACE, key, processed.path)
    return StoredFigure(
        key,
        processed.media_type,
        processed.width,
        processed.height,
        len(data),
        processed.sha256,
        data,
    )


_INSERT_CHUNK = text(
    """
    INSERT INTO chunks (id, generation_id, room_id, source_version_id, ordinal, kind, text, text_hash,
        token_count, extraction, ocr_confidence, flags, locator, page_start, page_end, section_key,
        embedding_dims, embedding)
    VALUES (:id, :gen, :room, :ver, :ordinal, :kind, :text, :hash, :tokens, :extraction, :conf,
        CAST(:flags AS jsonb), CAST(:locator AS jsonb), :ps, :pe, :section, :dims,
        CAST(:embedding AS vector))
    """
)
_INSERT_FIGURE = text(
    """
    INSERT INTO figures (chunk_id, generation_id, room_id, source_version_id, figure_number, page,
        storage_key, media_type, sha256, byte_size, width, height)
    VALUES (:chunk, :gen, :room, :ver, :number, :page, :key, :mt, :sha, :size, :w, :h)
    """
)


async def insert_chunks(
    sessionmaker: async_sessionmaker[AsyncSession],
    job: object,
    generation_id: uuid.UUID,
    rows: list[ChunkRow],
    vectors: list[list[float]],
    dims: int,
) -> None:
    """One batch of chunks with their vectors (and the figure rows of figure chunks), in one short
    transaction. The CHECK constraint refuses a vector whose dimension differs from `dims`."""
    assert len(rows) == len(vectors)  # noqa: S101 - caller bug if not
    chunk_params, figure_params = [], []
    for row, vector in zip(rows, vectors, strict=True):
        d = row.draft
        locator = {**d.locator, **row.locator_extra}
        flags = {"low_agreement": True} if d.low_agreement else {}
        chunk_params.append(
            {
                "id": row.id,
                "gen": generation_id,
                "room": job.room_id,  # type: ignore[attr-defined]
                "ver": job.source_version_id,  # type: ignore[attr-defined]
                "ordinal": d.ordinal,
                "kind": row.kind,
                "text": d.text,
                "hash": d.text_hash,
                "tokens": d.token_count,
                "extraction": d.extraction,
                "conf": d.ocr_confidence,
                "flags": json.dumps(flags),
                "locator": json.dumps(locator),
                "ps": d.page_start,
                "pe": d.page_end,
                "section": d.section_key or None,
                "dims": dims,
                "embedding": vector_literal(vector),
            }
        )
        if row.figure is not None and row.figure_number is not None:
            f = row.figure
            figure_params.append(
                {
                    "chunk": row.id,
                    "gen": generation_id,
                    "room": job.room_id,  # type: ignore[attr-defined]
                    "ver": job.source_version_id,  # type: ignore[attr-defined]
                    "number": row.figure_number,
                    "page": row.page,
                    "key": f.storage_key,
                    "mt": f.media_type,
                    "sha": f.sha256,
                    "size": f.byte_size,
                    "w": f.width,
                    "h": f.height,
                }
            )
    async with sessionmaker() as db, db.begin():
        await db.execute(_INSERT_CHUNK, chunk_params)
        if figure_params:
            await db.execute(_INSERT_FIGURE, figure_params)


@dataclass(frozen=True)
class Published:
    generation_id: uuid.UUID
    superseded: list[uuid.UUID]


async def publish(
    sessionmaker: async_sessionmaker[AsyncSession],
    job: object,
    token: uuid.UUID,
    generation_id: uuid.UUID,
    *,
    expected_chunks: int,
    expected_dims: int,
    summary: dict,
    warnings: list[dict],
    gaps: list[dict],
    pages_total: int | None,
    pages_unreadable: int,
    figures_found: int,
    figure_count: int,
) -> Published | None:
    """Validate, then switch the version's pointer. Returns None when the job no longer owns the
    version (claim lost, cancelled, or the source was removed meanwhile): nothing is published and
    the caller discards the building rows.

    Validation: every expected chunk exists, ordinals are 0..n-1, every chunk has a vector of exactly
    the generation's dimension, no text is empty (also a CHECK). Failing it raises IndexIncomplete.
    """
    async with sessionmaker() as db, db.begin():
        locked = await lock_room(db, job.room_id)  # first: the room, then the job and version rows
        owned = (
            await db.execute(
                text(
                    """SELECT cancel_requested_at FROM analysis_jobs
                        WHERE id = :j AND claim_token = :t AND state = 'running' FOR UPDATE"""
                ),
                {"j": job.id, "t": token},  # type: ignore[attr-defined]
            )
        ).one_or_none()
        ver = (
            await db.execute(
                select(_VER.c.analysis_state, _VER.c.current_generation_id)
                .where(_VER.c.id == job.source_version_id)  # type: ignore[attr-defined]
                .with_for_update()
            )
        ).one()
        if (
            owned is None
            or owned.cancel_requested_at is not None
            or ver.analysis_state == "removed"
        ):
            return None
        stats = (
            await db.execute(
                text(
                    """SELECT count(*) AS total, count(embedding) AS embedded,
                              count(*) FILTER (WHERE embedding_dims = :d) AS dims_ok,
                              coalesce(max(ordinal) + 1, 0) AS span,
                              count(DISTINCT ordinal) AS distinct_ordinals
                         FROM chunks WHERE generation_id = :g"""
                ),
                {"g": generation_id, "d": expected_dims},
            )
        ).one()
        if not (
            stats.total == expected_chunks
            and stats.total >= 1
            and stats.embedded == stats.total
            and stats.dims_ok == stats.total
            and stats.span == stats.total
            and stats.distinct_ordinals == stats.total
        ):
            raise IndexIncomplete
        old = (
            (
                await db.execute(
                    text(
                        """UPDATE index_generations SET state = 'superseded'
                            WHERE source_version_id = :v AND state = 'ready' AND id <> :g RETURNING id"""
                    ),
                    {"v": job.source_version_id, "g": generation_id},  # type: ignore[attr-defined]
                )
            )
            .scalars()
            .all()
        )
        await db.execute(
            text(
                """UPDATE index_generations SET state = 'ready', published_at = now(),
                       chunk_count = :n, figure_count = :fc, figures_found = :ff,
                       pages_total = :pt, pages_unreadable = :pu,
                       warnings = CAST(:w AS jsonb), gaps = CAST(:gaps AS jsonb)
                    WHERE id = :g"""
            ),
            {
                "g": generation_id,
                "n": stats.total,
                "fc": figure_count,
                "ff": figures_found,
                "pt": pages_total,
                "pu": pages_unreadable,
                "w": json.dumps(warnings),
                "gaps": json.dumps(gaps),
            },
        )
        await db.execute(
            update(_VER)
            .where(_VER.c.id == job.source_version_id)  # type: ignore[attr-defined]
            .values(current_generation_id=generation_id, analysis_state="ready", error_code=None)
        )
        await db.execute(
            text(
                """UPDATE analysis_jobs SET state = 'succeeded', claim_token = NULL,
                       lease_expires_at = NULL, finished_at = now(), stage = NULL,
                       result = CAST(:r AS jsonb) WHERE id = :j"""
            ),
            {"j": job.id, "r": json.dumps(summary)},  # type: ignore[attr-defined]
        )
        source_id = (
            await db.execute(select(_VER.c.source_id).where(_VER.c.id == job.source_version_id))  # type: ignore[attr-defined]
        ).scalar_one()
        if locked is not None:
            await emit_source_event(
                db,
                locked,
                source_id,
                job.source_version_id,
                "ready",
                job.requested_by,  # type: ignore[attr-defined]
            )
    return Published(generation_id, list(old))


async def discard_generations(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, ids: list[uuid.UUID]
) -> int:
    """Delete generations (their chunks, vectors and figure rows cascade) and the figure image files.
    A file that cannot be deleted now is an orphan that `cleanup_ai` removes."""
    if not ids:
        return 0
    async with sessionmaker() as db, db.begin():
        keys = (
            (
                await db.execute(
                    text(
                        "SELECT storage_key FROM figures WHERE generation_id = ANY(:ids) "
                        "AND storage_key IS NOT NULL"
                    ),
                    {"ids": ids},
                )
            )
            .scalars()
            .all()
        )
        await db.execute(
            text(
                "UPDATE source_versions SET current_generation_id = NULL "
                "WHERE current_generation_id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        await db.execute(text("DELETE FROM index_generations WHERE id = ANY(:ids)"), {"ids": ids})
    deleted = 0
    for key in keys:
        try:
            storage.delete(FIGURE_NAMESPACE, key)
            deleted += 1
        except OSError:
            log.warning("action=figure_file_delete_failed")
    return deleted


async def discard_building(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, job_id: uuid.UUID
) -> None:
    async with sessionmaker() as db:
        ids = list(
            (
                await db.execute(
                    text(
                        "SELECT id FROM index_generations WHERE job_id = :j AND state = 'building'"
                    ),
                    {"j": job_id},
                )
            )
            .scalars()
            .all()
        )
    await discard_generations(sessionmaker, storage, ids)
