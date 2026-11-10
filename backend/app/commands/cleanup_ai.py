"""AI cleanup: abandoned uploads, files of removed sources, orphan source files, stale scratch space.
Run it from cron / a scheduled job, like cleanup_documents.

    python -m app.commands.cleanup_ai [--batch-size 200]

Checkpoint 2 steps (every step is idempotent and safe to run concurrently or repeatedly; later
checkpoints add superseded/failed analysis generations, expired requests, manifests past retention
and expired budget windows to this same command):

  1. ABANDONED uploads: an upload still `intent`/`uploaded` after SOURCE_UPLOAD_TTL_MINUTES becomes
     `expired` in one atomic UPDATE that re-checks the state under the row lock (so a finalize that
     got there first wins), then its bytes are deleted;
  2. LEFTOVER bytes: `failed`/`cancelled`/`expired` uploads and `removed` source versions whose file
     could not be deleted at the time (the row keeps the storage key until the file is really gone)
     are retried;
  3. ORPHAN files: any file under STORAGE_PATH/sources that no source version and no upload names,
     and that is older than 24 hours (a crash between "file stored" and "row written"), is deleted;
  4. SCRATCH: `tmp/inspect-*` directories (a killed inspector or extractor) and `tmp/up-*` files
     older than one hour.

Checkpoint 3 steps:

  5. STALE `building` generations: left by a worker that died, and no queued or running job owns them
     any more (or they are over an hour old with no running job): deleted with their chunks, vectors
     and figure files;
  6. SUPERSEDED generations older than AI_SUPERSEDED_GENERATION_GRACE_HOURS (an old index outlives its
     replacement, so a request that pinned it keeps working). Checkpoint 4 adds "and no unfinished
     request pins it";
  7. ORPHAN figure images: files under STORAGE_PATH/figures that no `figures` row names and that are
     older than 24 hours.

Checkpoint 4 steps:

  8. MANIFESTS past retention: `ai_requests.retain_until` (finished + AI_RESULT_RETENTION_DAYS) has
     passed -> the manifest (retrieved passages, document captures, chat copies, records of images
     sent) is deleted. The published answer and its short citation excerpts stay.
  9. EXPIRED budget windows (older than 62 days), their warnings, and old rate-limit buckets.
  (step 6 now also keeps a superseded index that an unfinished request still pins)

The row is updated BEFORE its file is deleted, never the other way round, and the storage key is
cleared only after the file is gone.

Needs DATABASE_URL and STORAGE_PATH only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import time
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.analysis.store import FIGURE_NAMESPACE, discard_generations
from app.config import ConfigError, load_ai_cleanup_settings
from app.db import create_engine_from_url, create_sessionmaker
from app.models import SourceUpload, SourceVersion
from app.services import ai_proposals
from app.services.sources import NAMESPACE, OPEN_STATES, delete_removed_files
from app.storage import Storage

log = logging.getLogger("threadforge.cleanup_ai")
_UP = SourceUpload.__table__
_VER = SourceVersion.__table__
ORPHAN_AGE_SECONDS = 24 * 3600
SCRATCH_AGE_SECONDS = 3600


@dataclass(frozen=True)
class AiCleanupResult:
    uploads_expired: int = 0
    upload_files_deleted: int = 0
    removed_version_files_deleted: int = 0
    orphan_files: int = 0
    scratch_removed: int = 0
    generations_removed: int = 0
    orphan_figure_files: int = 0
    manifests_deleted: int = 0
    budget_rows_deleted: int = 0
    proposals_deleted: int = 0


async def _expire_uploads(sessionmaker: async_sessionmaker[AsyncSession], batch_size: int) -> int:
    total = 0
    while True:
        async with sessionmaker() as db, db.begin():
            victims = (
                select(_UP.c.id)
                .where(_UP.c.state.in_(OPEN_STATES), _UP.c.expires_at < func.clock_timestamp())
                .order_by(_UP.c.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            expired = (
                (
                    await db.execute(
                        update(_UP)
                        .where(
                            _UP.c.id.in_(victims),
                            _UP.c.state.in_(OPEN_STATES),  # re-checked under the row lock
                        )
                        .values(state="expired", finished_at=func.clock_timestamp())
                        .returning(_UP.c.id)
                    )
                )
                .scalars()
                .all()
            )
        total += len(expired)
        if len(expired) < batch_size:
            return total


async def _delete_terminal_upload_files(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, batch_size: int
) -> int:
    deleted = 0
    while True:
        async with sessionmaker() as db:
            rows = (
                await db.execute(
                    select(_UP.c.id, _UP.c.storage_key)
                    .where(
                        _UP.c.state.in_(("failed", "cancelled", "expired")),
                        _UP.c.storage_key.is_not(None),
                    )
                    .order_by(_UP.c.id)
                    .limit(batch_size)
                )
            ).all()
        progressed = 0
        for upload_id, key in rows:
            try:
                existed = storage.delete(NAMESPACE, key)
            except OSError:
                log.warning("could not delete an upload file; it will be retried")
                continue
            async with sessionmaker() as db, db.begin():
                await db.execute(
                    update(_UP)
                    .where(_UP.c.id == upload_id, _UP.c.storage_key == key)
                    .values(storage_key=None)
                )
            deleted += 1 if existed else 0
            progressed += 1
        if len(rows) < batch_size or progressed == 0:
            return deleted


async def _delete_removed_version_files(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, batch_size: int
) -> int:
    total = 0
    while True:
        async with sessionmaker() as db:
            keys = (
                (
                    await db.execute(
                        select(_VER.c.storage_key)
                        .where(_VER.c.analysis_state == "removed", _VER.c.storage_key.is_not(None))
                        .order_by(_VER.c.id)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
        done = await delete_removed_files(sessionmaker, storage, list(keys))
        total += done
        if len(keys) < batch_size or done == 0:
            return total


async def _sweep_orphans(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, now: float, batch_size: int
) -> int:
    candidates = [
        key
        for key, _path, mtime in storage.iter_files(NAMESPACE)
        if now - mtime > ORPHAN_AGE_SECONDS
    ]
    orphans = 0
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        async with sessionmaker() as db:
            used = set(
                (await db.execute(select(_VER.c.storage_key).where(_VER.c.storage_key.in_(chunk))))
                .scalars()
                .all()
            ) | set(
                (await db.execute(select(_UP.c.storage_key).where(_UP.c.storage_key.in_(chunk))))
                .scalars()
                .all()
            )
        for key in chunk:
            if key not in used and storage.delete(NAMESPACE, key):
                orphans += 1
    return orphans


def _sweep_scratch(storage: Storage, now: float) -> int:
    base = storage.root / "tmp"
    removed = 0
    if not base.is_dir():
        return 0
    for path in base.iterdir():
        try:
            if now - path.stat().st_mtime <= SCRATCH_AGE_SECONDS:
                continue
            if path.is_dir() and path.name.startswith("inspect-"):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            elif path.is_file() and path.name.startswith("up-"):
                path.unlink()
                removed += 1
        except OSError:
            log.warning("could not remove stale scratch space")
    return removed


async def _remove_stale_generations(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    grace_hours: int,
    batch_size: int,
) -> int:
    """Steps 5 and 6. Each batch is selected and deleted by generation id; the state is re-read by the
    DELETE inside `discard_generations`' own transaction semantics (rows are simply gone or not)."""
    total = 0
    while True:
        async with sessionmaker() as db:
            ids = list(
                (
                    await db.execute(
                        text(
                            """
                            SELECT g.id FROM index_generations g
                             WHERE (g.state = 'building'
                                    AND g.created_at < now() - interval '1 hour'
                                    AND NOT EXISTS (SELECT 1 FROM analysis_jobs j
                                                     WHERE j.id = g.job_id AND j.state = 'running'
                                                       AND j.lease_expires_at >= now()))
                                OR g.state = 'failed'
                                OR (g.state = 'superseded'
                                    AND g.published_at < now() - make_interval(hours => :grace)
                                    AND NOT EXISTS (
                                        SELECT 1 FROM ai_request_sources rs
                                          JOIN ai_requests r ON r.id = rs.request_id
                                         WHERE rs.generation_id = g.id
                                           AND r.state IN ('waiting_for_sources', 'queued', 'running',
                                                           'validating', 'cancelling')))
                             ORDER BY g.created_at LIMIT :n
                            """
                        ),
                        {"grace": grace_hours, "n": batch_size},
                    )
                )
                .scalars()
                .all()
            )
        if not ids:
            return total
        await discard_generations(sessionmaker, storage, ids)
        total += len(ids)
        if len(ids) < batch_size:
            return total


async def _delete_old_manifests(
    sessionmaker: async_sessionmaker[AsyncSession], batch_size: int
) -> int:
    total = 0
    while True:
        async with sessionmaker() as db, db.begin():
            done = (
                await db.execute(
                    text(
                        """DELETE FROM ai_manifests m USING (
                               SELECT m2.request_id, m2.attempt FROM ai_manifests m2
                                 JOIN ai_requests r ON r.id = m2.request_id
                                WHERE r.retain_until IS NOT NULL AND r.retain_until < now()
                                  AND NOT EXISTS (
                                      SELECT 1 FROM ai_proposals p
                                       WHERE p.request_id = r.id
                                         AND p.state IN ('pending', 'applying'))
                                LIMIT :n) old
                            WHERE m.request_id = old.request_id AND m.attempt = old.attempt"""
                    ),
                    {"n": batch_size},
                )
            ).rowcount
        total += done
        if done < batch_size:
            return total


async def _delete_expired_budget_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as db, db.begin():
        n = 0
        for table, column, age in (
            ("ai_budget_windows", "window_start", "current_date - 62"),
            ("ai_budget_warnings", "window_start", "current_date - 62"),
            ("ai_rate_buckets", "window_start", "now() - interval '2 days'"),
        ):
            n += (await db.execute(text(f"DELETE FROM {table} WHERE {column} < {age}"))).rowcount  # noqa: S608
    return n


async def _sweep_orphan_figures(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, now: float, batch_size: int
) -> int:
    candidates = [
        key
        for key, _path, mtime in storage.iter_files(FIGURE_NAMESPACE)
        if now - mtime > ORPHAN_AGE_SECONDS
    ]
    orphans = 0
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        async with sessionmaker() as db:
            used = set(
                (
                    await db.execute(
                        text("SELECT storage_key FROM figures WHERE storage_key = ANY(:k)"),
                        {"k": chunk},
                    )
                )
                .scalars()
                .all()
            )
        for key in chunk:
            if key not in used and storage.delete(FIGURE_NAMESPACE, key):
                orphans += 1
    return orphans


async def cleanup_ai(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    batch_size: int = 200,
    now: float | None = None,
    grace_hours: int = 24,
    proposal_retention_days: int = 30,
) -> AiCleanupResult:
    now = time.time() if now is None else now
    expired = await _expire_uploads(sessionmaker, batch_size)
    upload_files = await _delete_terminal_upload_files(sessionmaker, storage, batch_size)
    version_files = await _delete_removed_version_files(sessionmaker, storage, batch_size)
    orphans = await _sweep_orphans(sessionmaker, storage, now, batch_size)
    scratch = await asyncio.to_thread(_sweep_scratch, storage, now)
    generations = await _remove_stale_generations(sessionmaker, storage, grace_hours, batch_size)
    orphan_figures = await _sweep_orphan_figures(sessionmaker, storage, now, batch_size)
    manifests = await _delete_old_manifests(sessionmaker, batch_size)
    budget_rows = await _delete_expired_budget_rows(sessionmaker)
    proposals = await ai_proposals.delete_old(sessionmaker, proposal_retention_days, batch_size)
    return AiCleanupResult(
        expired,
        upload_files,
        version_files,
        orphans,
        scratch,
        generations,
        orphan_figures,
        manifests,
        budget_rows,
        proposals,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean up AI uploads, sources and scratch space.")
    parser.add_argument("--batch-size", type=int, default=200)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> AiCleanupResult:
    settings = load_ai_cleanup_settings()
    engine = create_engine_from_url(settings.database_url.get_secret_value())
    try:
        return await cleanup_ai(
            create_sessionmaker(engine),
            Storage(settings.storage_path),
            batch_size=args.batch_size,
            grace_hours=settings.ai_superseded_generation_grace_hours,
            proposal_retention_days=settings.ai_proposal_retention_days,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.batch_size < 1:
        print("error: batch size must be >= 1", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(_run(args))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"expired {result.uploads_expired} abandoned uploads; deleted "
        f"{result.upload_files_deleted} upload files, {result.removed_version_files_deleted} files "
        f"of removed sources, {result.orphan_files} orphan files; removed {result.scratch_removed} "
        f"stale scratch items, {result.generations_removed} stale index generations and "
        f"{result.orphan_figure_files} orphan figure files; deleted {result.manifests_deleted} "
        f"manifests past retention and {result.budget_rows_deleted} expired budget/rate rows"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
