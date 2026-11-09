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
  4. SCRATCH: `tmp/inspect-*` directories (a killed inspector) and `tmp/up-*` files older than one
     hour.

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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ConfigError, load_ai_cleanup_settings
from app.db import create_engine_from_url, create_sessionmaker
from app.models import SourceUpload, SourceVersion
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


async def cleanup_ai(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    batch_size: int = 200,
    now: float | None = None,
) -> AiCleanupResult:
    now = time.time() if now is None else now
    expired = await _expire_uploads(sessionmaker, batch_size)
    upload_files = await _delete_terminal_upload_files(sessionmaker, storage, batch_size)
    version_files = await _delete_removed_version_files(sessionmaker, storage, batch_size)
    orphans = await _sweep_orphans(sessionmaker, storage, now, batch_size)
    scratch = await asyncio.to_thread(_sweep_scratch, storage, now)
    return AiCleanupResult(expired, upload_files, version_files, orphans, scratch)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean up AI uploads, sources and scratch space.")
    parser.add_argument("--batch-size", type=int, default=200)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> AiCleanupResult:
    settings = load_ai_cleanup_settings()
    engine = create_engine_from_url(settings.database_url.get_secret_value())
    try:
        return await cleanup_ai(
            create_sessionmaker(engine), Storage(settings.storage_path), batch_size=args.batch_size
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
        f"stale scratch items"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
