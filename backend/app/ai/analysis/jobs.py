"""The analysis job queue: enqueue, claim with a lease, renew, cancel, finish.

TERMS
  * job      one row in `analysis_jobs`: "analyze THIS source version with THIS configuration".
  * queue    the table itself. A worker takes a job with one UPDATE ... FOR UPDATE SKIP LOCKED, so two
             workers can never take the same job and nobody waits for a row another worker holds.
  * claim token / lease   when a worker takes a job it writes a random token and a deadline
             (`lease_expires_at`). It must renew the deadline between stages and present the token for
             every later write. A worker that crashes stops renewing; when the deadline passes any
             worker may take the job again (new token, `attempts` + 1). The old worker, if it is only
             slow and wakes up, finds its token gone and stops: it can no longer change anything.
  * waiter   who is waiting for the job (a chat request, or a person who pressed "Analyze now"). Two
             requests that attach the same file share ONE job; if the last waiter leaves before the
             job runs, the job is cancelled.

The unique partial index `uq_analysis_jobs_active (source_version_id, config_fingerprint) WHERE state IN
('queued','running')` is what makes "one shared job" true even when two requests race.

Fair claim order: jobs of the room with the fewest jobs running right now go first, oldest first inside
that, so one room uploading fifty files cannot starve the others.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.analysis.chunking import CHUNK_POLICY
from app.config import Settings
from app.logging_config import APP_LOGGER
from app.models import AnalysisJob, IndexGeneration, SourceVersion

log = logging.getLogger(f"{APP_LOGGER}.ai.jobs")
_JOBS = AnalysisJob.__table__
_VER = SourceVersion.__table__
_GEN = IndexGeneration.__table__
PARSER_VERSION = "extract-v1"

# job.stage -> what users see in source_versions.analysis_state
STAGE_TO_STATE = {
    "extracting": "extracting",
    "ocr": "extracting",
    "figures": "extracting",
    "chunking": "extracting",
    "embedding": "indexing",
    "publishing": "indexing",
}
TERMINAL = ("succeeded", "failed", "cancelled")


def config_fingerprint(settings: Settings) -> str:
    """Everything that changes WHAT an index contains. If it differs from a stored generation's, that
    generation is stale and attaching the file builds a new one."""
    payload = {
        "parser": PARSER_VERSION,
        "chunk": CHUNK_POLICY,
        "chunk_tokens": [
            settings.ai_chunk_target_tokens,
            settings.ai_chunk_max_tokens,
            settings.ai_chunk_overlap_tokens,
        ],
        "embed": [
            settings.ai_embed_model,
            settings.ai_embed_dimensions,
            settings.ai_embed_normalize,
            settings.ai_embed_document_prefix,
        ],
        "vision": settings.ai_llm_model,
        "ocr": [
            settings.ai_ocr_dpi,
            settings.ai_ocr_min_confidence,
            settings.ai_ocr_min_words_per_page,
            settings.ai_ocr_max_implausible_share,
        ],
        "figures": [
            settings.ai_figure_embedding_mode,
            settings.ai_figure_min_pixels,
            settings.ai_figure_max_repeats,
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def caps_snapshot(settings: Settings) -> dict:
    """The limits this job runs under, frozen at admission (changing a setting later does not change
    a queued job)."""
    figures = min(settings.ai_max_new_analysis_figures, settings.ai_max_figures_per_source)
    vision_pages = settings.ai_vision_fallback_max_pages
    return {
        "max_pages": settings.ai_max_new_analysis_pages,
        "max_figures": figures,
        "max_vision_pages": vision_pages,
        "max_provider_calls": settings.ai_analysis_max_provider_calls,
        # every vision page and every figure sends one image (+ image embeddings when configured)
        "max_images": (vision_pages + figures) * 2 + 8,
    }


@dataclass(frozen=True)
class Enqueued:
    status: str  # ready | created | joined
    job_id: uuid.UUID | None
    generation_id: uuid.UUID | None


async def enqueue(
    db: AsyncSession,
    settings: Settings,
    *,
    room_id: uuid.UUID,
    version_id: uuid.UUID,
    requested_by: uuid.UUID,
    anchor_document_id: uuid.UUID,
    key_source_generation: str,
    key_source_embedding: str,
    waiter_id: uuid.UUID,
    waiter_kind: str,
) -> Enqueued:
    """Inside the caller's transaction (which holds the room lock and has already checked the gate).

    * the version already has a READY index built with this configuration -> `ready`, no job, no
      provider call (this is the "later attaches reuse it" rule);
    * a queued or running job exists for it -> the waiter joins it (`joined`);
    * otherwise a new job is created (`created`), and the version shows `queued`.
    """
    fingerprint = config_fingerprint(settings)
    ver = (
        await db.execute(
            select(_VER.c.id, _VER.c.analysis_state, _VER.c.current_generation_id)
            .where(_VER.c.id == version_id, _VER.c.room_id == room_id)
            .with_for_update()
        )
    ).one()
    if ver.current_generation_id is not None:
        ready = (
            await db.execute(
                select(_GEN.c.id).where(
                    _GEN.c.id == ver.current_generation_id,
                    _GEN.c.state == "ready",
                    _GEN.c.config_fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        if ready is not None:
            return Enqueued("ready", None, ready)
    created = (
        await db.execute(
            text(
                """
                INSERT INTO analysis_jobs (room_id, source_version_id, config_fingerprint, state,
                    requested_by, anchor_document_id, charged_user_id, key_source_generation,
                    key_source_embedding, caps, max_attempts)
                VALUES (:room, :ver, :fp, 'queued', :by, :anchor, :by, :kg, :ke,
                    CAST(:caps AS jsonb), :max)
                ON CONFLICT (source_version_id, config_fingerprint) WHERE state IN ('queued', 'running')
                DO NOTHING RETURNING id
                """
            ),
            {
                "room": room_id,
                "ver": version_id,
                "fp": fingerprint,
                "by": requested_by,
                "anchor": anchor_document_id,
                "kg": key_source_generation,
                "ke": key_source_embedding,
                "caps": json.dumps(caps_snapshot(settings)),
                "max": settings.ai_analysis_max_attempts,
            },
        )
    ).scalar_one_or_none()
    if created is not None:
        job_id, status = created, "created"
        await db.execute(
            update(_VER)
            .where(_VER.c.id == version_id, _VER.c.analysis_state != "removed")
            .values(analysis_state="queued", error_code=None)
        )
    else:
        job_id = (
            await db.execute(
                select(_JOBS.c.id).where(
                    _JOBS.c.source_version_id == version_id,
                    _JOBS.c.config_fingerprint == fingerprint,
                    _JOBS.c.state.in_(("queued", "running")),
                )
            )
        ).scalar_one()
        status = "joined"
    await db.execute(
        text(
            """INSERT INTO analysis_job_waiters (job_id, waiter_id, kind, user_id)
               VALUES (:job, :waiter, :kind, :user) ON CONFLICT DO NOTHING"""
        ),
        {"job": job_id, "waiter": waiter_id, "kind": waiter_kind, "user": requested_by},
    )
    return Enqueued(status, job_id, None)


async def release_waiter_in(db: AsyncSession, waiter_id: uuid.UUID) -> int:
    """In the caller's transaction: a request (or an Analyze-now click) no longer wants its files
    analyzed. Every job it waited for that has NO other waiter is cancelled: a queued one right away, a
    running one at its next stage boundary. A job someone else still waits for is kept. Returns the
    jobs cancelled."""
    cancelled = 0
    job_ids = (
        (
            await db.execute(
                text("DELETE FROM analysis_job_waiters WHERE waiter_id = :w RETURNING job_id"),
                {"w": waiter_id},
            )
        )
        .scalars()
        .all()
    )
    for job_id in job_ids:
        job = (
            await db.execute(
                select(_JOBS.c.id, _JOBS.c.state, _JOBS.c.source_version_id)
                .where(_JOBS.c.id == job_id)
                .with_for_update()
            )
        ).one_or_none()
        if job is None or job.state not in ("queued", "running"):
            continue
        left = (
            await db.execute(
                text("SELECT count(*) FROM analysis_job_waiters WHERE job_id = :j"), {"j": job_id}
            )
        ).scalar_one()
        if left:
            continue
        cancelled += 1
        if job.state == "queued":
            await _finish_row(db, job_id, "cancelled", None)
            await _reset_version(db, job.source_version_id)
        else:
            await db.execute(
                update(_JOBS).where(_JOBS.c.id == job_id).values(cancel_requested_at=text("now()"))
            )
    return cancelled


async def release_waiter(
    sessionmaker: async_sessionmaker[AsyncSession], waiter_id: uuid.UUID
) -> int:
    """`release_waiter_in` in its own transaction."""
    async with sessionmaker() as db, db.begin():
        return await release_waiter_in(db, waiter_id)


async def _reset_version(db: AsyncSession, version_id: uuid.UUID) -> None:
    """A version whose job ended without a ready index goes back to what it was: ready if an index
    is still current, else not_analyzed."""
    await db.execute(
        text(
            """UPDATE source_versions SET analysis_state =
                 CASE WHEN current_generation_id IS NOT NULL THEN 'ready' ELSE 'not_analyzed' END
               WHERE id = :v AND analysis_state NOT IN ('removed', 'ready')
                 AND NOT EXISTS (SELECT 1 FROM analysis_jobs
                                  WHERE source_version_id = :v AND state IN ('queued', 'running'))"""
        ),
        {"v": version_id},
    )


async def _finish_row(
    db: AsyncSession,
    job_id: uuid.UUID,
    state: str,
    error_code: str | None,
    result: dict | None = None,
) -> None:
    await db.execute(
        text(
            """UPDATE analysis_jobs SET state = :s, error_code = :e, claim_token = NULL,
                   lease_expires_at = NULL, finished_at = now(), stage = NULL,
                   result = CAST(:r AS jsonb)
               WHERE id = :j"""
        ),
        {"s": state, "e": error_code, "j": job_id, "r": json.dumps(result) if result else None},
    )


# ---- claim / lease ------------------------------------------------------------------------------------


_CLAIM = text(
    """
    UPDATE analysis_jobs SET state = 'running', claim_token = :token,
           lease_expires_at = now() + make_interval(secs => :lease),
           attempts = attempts + 1, started_at = COALESCE(started_at, now()), stage = 'extracting'
     WHERE id = (
        SELECT j.id FROM analysis_jobs j
         WHERE j.attempts < j.max_attempts AND j.cancel_requested_at IS NULL
           AND (   (j.state = 'queued' AND j.run_after <= now())
                OR (j.state = 'running' AND j.lease_expires_at < now()))
         ORDER BY (SELECT count(*) FROM analysis_jobs r
                    WHERE r.room_id = j.room_id AND r.state = 'running'
                      AND r.lease_expires_at >= now()),
                  j.created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
    RETURNING *
    """
)


async def claim_next(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[object, uuid.UUID] | None:
    """Take the next job (or one whose worker disappeared). Returns (job row, claim token)."""
    token = uuid.uuid4()
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(_CLAIM, {"token": token, "lease": settings.ai_analysis_lease_seconds})
        ).one_or_none()
    return (row, token) if row is not None else None


async def sweep_abandoned(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Running jobs whose worker is gone (lease expired) and that must NOT be retried: those whose
    attempts are used up (the worker kept crashing) end `failed`, those whose last waiter left end
    `cancelled`. Everything else with an expired lease is simply claimed again by `claim_next`."""
    async with sessionmaker() as db, db.begin():
        rows = (
            await db.execute(
                text(
                    """SELECT id, source_version_id, cancel_requested_at IS NOT NULL AS cancelled
                         FROM analysis_jobs
                        WHERE state = 'running' AND lease_expires_at < now()
                          AND (attempts >= max_attempts OR cancel_requested_at IS NOT NULL)
                        FOR UPDATE SKIP LOCKED"""
                )
            )
        ).all()
        for row in rows:
            if row.cancelled:
                await _finish_row(db, row.id, "cancelled", None)
                await _reset_version(db, row.source_version_id)
            else:
                await _finish_row(db, row.id, "failed", "attempts_exhausted")
                await _fail_version(db, row.source_version_id, "attempts_exhausted")
    return len(rows)


async def _fail_version(db: AsyncSession, version_id: uuid.UUID, code: str) -> None:
    await db.execute(
        update(_VER)
        .where(_VER.c.id == version_id, _VER.c.analysis_state.notin_(("removed", "ready")))
        .values(analysis_state="failed", error_code=code)
    )


async def renew(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_id: uuid.UUID,
    token: uuid.UUID,
    *,
    stage: str | None = None,
) -> str | None:
    """Between stages: extend the lease, record the stage, show the matching state on the version.
    Returns None while the job is still ours and wanted, else why not:
    `lost_claim` | `cancelled` | `source_removed`."""
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                text(
                    """UPDATE analysis_jobs SET lease_expires_at = now() + make_interval(secs => :lease),
                              stage = COALESCE(CAST(:stage AS text), stage)
                        WHERE id = :j AND claim_token = :t AND state = 'running'
                        RETURNING cancel_requested_at, source_version_id"""
                ),
                {
                    "lease": settings.ai_analysis_lease_seconds,
                    "stage": stage,
                    "j": job_id,
                    "t": token,
                },
            )
        ).one_or_none()
        if row is None:
            return "lost_claim"
        state = (
            await db.execute(
                select(_VER.c.analysis_state).where(_VER.c.id == row.source_version_id)
            )
        ).scalar_one()
        if state == "removed":
            return "source_removed"
        if row.cancel_requested_at is not None:
            return "cancelled"
        if stage is not None:
            await db.execute(
                update(_VER)
                .where(_VER.c.id == row.source_version_id, _VER.c.analysis_state != "removed")
                .values(analysis_state=STAGE_TO_STATE[stage])
            )
    return None


async def still_wanted(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID, token: uuid.UUID
) -> str | None:
    """Read-only version of `renew` for the checks made before every provider call."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                text(
                    """SELECT j.cancel_requested_at, v.analysis_state
                         FROM analysis_jobs j JOIN source_versions v ON v.id = j.source_version_id
                        WHERE j.id = :j AND j.claim_token = :t AND j.state = 'running'"""
                ),
                {"j": job_id, "t": token},
            )
        ).one_or_none()
    if row is None:
        return "lost_claim"
    if row.analysis_state == "removed":
        return "source_removed"
    if row.cancel_requested_at is not None:
        return "cancelled"
    return None


async def finish_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    token: uuid.UUID,
    version_id: uuid.UUID,
    code: str,
) -> bool:
    """The job failed for good: say why on the job and on the version (and announce it). False if the
    claim was lost. The room is locked first, like every writer that records an event."""
    from app.services.room_lock import lock_room
    from app.services.sources import emit_source_event

    async with sessionmaker() as db, db.begin():
        info = (
            await db.execute(
                text(
                    "SELECT j.room_id, j.requested_by, v.source_id FROM analysis_jobs j "
                    "JOIN source_versions v ON v.id = j.source_version_id WHERE j.id = :j"
                ),
                {"j": job_id},
            )
        ).one_or_none()
        if info is None:
            return False
        locked = await lock_room(db, info.room_id)
        owned = (
            await db.execute(
                text("SELECT 1 FROM analysis_jobs WHERE id = :j AND claim_token = :t FOR UPDATE"),
                {"j": job_id, "t": token},
            )
        ).first()
        if owned is None:
            return False
        await _finish_row(db, job_id, "failed", code)
        await _fail_version(db, version_id, code)
        if locked is not None:
            await emit_source_event(
                db, locked, info.source_id, version_id, "failed", info.requested_by
            )
    return True


async def finish_cancelled(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    token: uuid.UUID,
    version_id: uuid.UUID,
) -> None:
    async with sessionmaker() as db, db.begin():
        owned = (
            await db.execute(
                text("SELECT 1 FROM analysis_jobs WHERE id = :j AND claim_token = :t FOR UPDATE"),
                {"j": job_id, "t": token},
            )
        ).first()
        if owned is None:
            return
        await _finish_row(db, job_id, "cancelled", None)
        await _reset_version(db, version_id)


async def requeue(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_id: uuid.UUID,
    token: uuid.UUID,
    version_id: uuid.UUID,
    code: str,
) -> bool:
    """A transient failure (the provider was down after the call policy gave up): try the whole job
    again later, unless the attempts are used up (then it fails). Returns True if requeued."""
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                text(
                    "SELECT attempts, max_attempts FROM analysis_jobs "
                    "WHERE id = :j AND claim_token = :t FOR UPDATE"
                ),
                {"j": job_id, "t": token},
            )
        ).one_or_none()
        if row is None:
            return False
        if row.attempts >= row.max_attempts:
            await _finish_row(db, job_id, "failed", code)
            await _fail_version(db, version_id, code)
            return False
        delay = timedelta(seconds=min(300, 30 * 2 ** (row.attempts - 1)))
        await db.execute(
            text(
                """UPDATE analysis_jobs SET state = 'queued', claim_token = NULL, lease_expires_at = NULL,
                          stage = NULL, error_code = :e, run_after = now() + :d WHERE id = :j"""
            ),
            {"e": code, "d": delay, "j": job_id},
        )
        await db.execute(
            update(_VER)
            .where(_VER.c.id == version_id, _VER.c.analysis_state != "removed")
            .values(analysis_state="queued")
        )
    return True
