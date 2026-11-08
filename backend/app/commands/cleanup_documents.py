"""Delete expired document assets and stray files. Run it from cron / a scheduled job.

    python -m app.commands.cleanup_documents [--batch-size 500]

What it does (every step is idempotent and safe to run concurrently or repeatedly):
  1. PENDING assets older than ASSET_PENDING_TTL_HOURS (default 24) that no save ever referenced
     become `expired`, and their bytes are deleted;
  2. ATTACHED assets that no document references any more (unreferenced_since is set by the save
     that dropped them) and that have been unreferenced for ASSET_UNREFERENCED_GRACE_DAYS (7)
     become `expired`, and their bytes are deleted. The grace period exists so a client undo shortly
     after a save still finds its image; an undo across that window may show a broken image;
  3. ORPHAN files: any file under STORAGE_PATH/assets with no live (non-expired) row, older than 24
     hours, is deleted (a crash between "store the file" and "insert the row", or a failed delete in
     step 1 or 2);
  4. stale temporary files in STORAGE_PATH/tmp older than one hour (an interrupted upload).

The row is marked `expired` BEFORE its file is deleted, in one atomic UPDATE that re-checks the
conditions (so a save that re-referenced the asset a moment earlier wins), never the other way
round. Removing an image from UNSAVED local content never deletes anything.

Needs DATABASE_URL and STORAGE_PATH (and the two lifecycle settings), not the other settings.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ConfigError, load_document_cleanup_settings
from app.db import create_engine_from_url, create_sessionmaker
from app.models import DocumentAssetRef, RoomAsset
from app.storage import Storage

log = logging.getLogger("threadforge.cleanup")
_ASSETS = RoomAsset.__table__
_REFS = DocumentAssetRef.__table__
ORPHAN_AGE_SECONDS = 24 * 3600
TEMP_AGE_SECONDS = 3600


@dataclass(frozen=True)
class DocumentCleanupResult:
    pending_expired: int = 0
    unreferenced_expired: int = 0
    orphan_files: int = 0
    temp_files: int = 0


async def _expire(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, condition, batch_size: int
) -> int:
    total = 0
    while True:
        async with sessionmaker() as db, db.begin():
            victims = (
                select(_ASSETS.c.id)
                .where(condition)
                .order_by(_ASSETS.c.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(
                update(_ASSETS)
                .where(_ASSETS.c.id.in_(victims), condition)  # re-checked under the row lock
                .values(state="expired")
                .returning(_ASSETS.c.storage_key)
            )
            keys = result.scalars().all()
        for key in keys:  # the transaction is over: file work happens outside it
            try:
                storage.delete("assets", key)
            except OSError:
                log.warning("could not delete an expired asset file; the orphan sweep will retry")
        total += len(keys)
        if len(keys) < batch_size:
            return total


async def cleanup_documents(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    pending_ttl_hours: int = 24,
    grace_days: int = 7,
    batch_size: int = 500,
    now: float | None = None,
) -> DocumentCleanupResult:
    now = time.time() if now is None else now
    pending = await _expire(
        sessionmaker,
        storage,
        (_ASSETS.c.state == "pending")
        & (_ASSETS.c.created_at < func.clock_timestamp() - timedelta(hours=pending_ttl_hours)),
        batch_size,
    )
    unreferenced = await _expire(
        sessionmaker,
        storage,
        (_ASSETS.c.state == "attached")
        & (_ASSETS.c.unreferenced_since.is_not(None))
        & (_ASSETS.c.unreferenced_since < func.clock_timestamp() - timedelta(days=grace_days))
        & ~exists().where(_REFS.c.asset_id == _ASSETS.c.id),
        batch_size,
    )

    orphans = 0
    candidates = [
        (key, path)
        for key, path, mtime in storage.iter_files("assets")
        if now - mtime > ORPHAN_AGE_SECONDS
    ]
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        async with sessionmaker() as db:
            rows = await db.execute(
                select(_ASSETS.c.storage_key).where(
                    _ASSETS.c.storage_key.in_([k for k, _ in chunk]), _ASSETS.c.state != "expired"
                )
            )
            live = set(rows.scalars().all())
        for key, _path in chunk:
            if key not in live and storage.delete("assets", key):
                orphans += 1

    temps = 0
    for path in list(storage.stale_temp_files(TEMP_AGE_SECONDS, now)):
        try:
            path.unlink()
            temps += 1
        except OSError:
            log.warning("could not remove a stale temporary file")
    return DocumentCleanupResult(pending, unreferenced, orphans, temps)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expire unused document assets, remove stray files."
    )
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> DocumentCleanupResult:
    settings = load_document_cleanup_settings()
    engine = create_engine_from_url(settings.database_url.get_secret_value())
    try:
        return await cleanup_documents(
            create_sessionmaker(engine),
            Storage(settings.storage_path),
            pending_ttl_hours=settings.asset_pending_ttl_hours,
            grace_days=settings.asset_unreferenced_grace_days,
            batch_size=args.batch_size,
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
        f"expired {result.pending_expired} unused pending and {result.unreferenced_expired} "
        f"unreferenced assets; deleted {result.orphan_files} orphan files and "
        f"{result.temp_files} stale temporary files"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
