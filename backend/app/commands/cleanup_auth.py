"""Delete stale authentication rows. Run it from cron / a scheduled job; there is no built-in
scheduler or task queue in this part.

    python -m app.commands.cleanup_auth [--batch-size 1000] [--expired-retention-days 1]
                                        [--revoked-retention-days 7]

Deletes, in batches (each batch is its own short transaction, so it never holds long locks):
  * oidc_login_attempts whose expires_at has passed (consumed or not);
  * sessions that expired more than --expired-retention-days ago;
  * sessions revoked more than --revoked-retention-days ago;
  * message_write_buckets (rate-limit counters) whose window started more than 24 hours ago;
  * PROCESSED outbox rows older than OUTBOX_PROCESSED_RETENTION_DAYS (default 7;
    override with --outbox-retention-days).
Messages, room_events and FAILED outbox rows are NEVER deleted here. Live sessions, users and
identities are never touched. Safe to run concurrently and repeatedly (rows are claimed with
FOR UPDATE SKIP LOCKED).

Needs only DATABASE_URL, not the other application settings.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import ColumnElement, Table, delete, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ConfigError, load_cleanup_settings
from app.db import create_engine_from_url, create_sessionmaker
from app.models import MessageWriteBucket, OIDCLoginAttempt, OutboxEntry, Session

log = logging.getLogger("threadforge.cleanup")

DEFAULT_BATCH_SIZE = 1000
DEFAULT_EXPIRED_RETENTION_DAYS = 1
DEFAULT_REVOKED_RETENTION_DAYS = 7
DEFAULT_OUTBOX_RETENTION_DAYS = 7
BUCKET_RETENTION_HOURS = 24

_ATTEMPTS = OIDCLoginAttempt.__table__
_SESSIONS = Session.__table__
_BUCKETS = MessageWriteBucket.__table__
_OUTBOX = OutboxEntry.__table__


@dataclass(frozen=True)
class CleanupResult:
    login_attempts: int
    sessions: int
    message_write_buckets: int = 0
    outbox_processed: int = 0


async def _delete_in_batches(
    sessionmaker: async_sessionmaker[AsyncSession],
    table: Table,
    condition: ColumnElement[bool],
    batch_size: int,
) -> int:
    total = 0
    while True:
        async with sessionmaker() as db, db.begin():
            victims = (
                select(table.c.id)
                .where(condition)
                .order_by(table.c.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            deleted = (await db.execute(delete(table).where(table.c.id.in_(victims)))).rowcount
        total += deleted
        if deleted < batch_size:
            return total


async def _delete_ctid_batches(
    sessionmaker: async_sessionmaker[AsyncSession],
    table: Table,
    condition: ColumnElement[bool],
    batch_size: int,
) -> int:
    """Batched delete for tables without a single id column (composite or non-id keys): rows are
    claimed by ctid (PostgreSQL's physical row id, stable within one statement)."""
    ctid = literal_column("ctid")
    total = 0
    while True:
        async with sessionmaker() as db, db.begin():
            victims = (
                select(ctid)
                .select_from(table)
                .where(condition)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            deleted = (await db.execute(delete(table).where(ctid.in_(victims)))).rowcount
        total += deleted
        if deleted < batch_size:
            return total


async def cleanup_auth(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    expired_retention_days: int = DEFAULT_EXPIRED_RETENTION_DAYS,
    revoked_retention_days: int = DEFAULT_REVOKED_RETENTION_DAYS,
    outbox_retention_days: int = DEFAULT_OUTBOX_RETENTION_DAYS,
) -> CleanupResult:
    if (
        batch_size < 1
        or expired_retention_days < 0
        or revoked_retention_days < 0
        or outbox_retention_days < 0
    ):
        raise ValueError("batch size must be >= 1 and retention days must be >= 0")

    attempts = await _delete_in_batches(
        sessionmaker, _ATTEMPTS, _ATTEMPTS.c.expires_at <= func.now(), batch_size
    )
    sessions = await _delete_in_batches(
        sessionmaker,
        _SESSIONS,
        or_(
            _SESSIONS.c.expires_at <= func.now() - timedelta(days=expired_retention_days),
            _SESSIONS.c.revoked_at <= func.now() - timedelta(days=revoked_retention_days),
        ),
        batch_size,
    )
    buckets = await _delete_ctid_batches(
        sessionmaker,
        _BUCKETS,
        _BUCKETS.c.window_start < func.now() - timedelta(hours=BUCKET_RETENTION_HOURS),
        batch_size,
    )
    # ONLY processed rows past retention: pending/processing work and failed rows (an operator's
    # to-do list) are never touched, and neither are the room_events they reference.
    outbox_processed = await _delete_ctid_batches(
        sessionmaker,
        _OUTBOX,
        (_OUTBOX.c.state == "processed")
        & (_OUTBOX.c.processed_at < func.now() - timedelta(days=outbox_retention_days)),
        batch_size,
    )
    return CleanupResult(
        login_attempts=attempts,
        sessions=sessions,
        message_write_buckets=buckets,
        outbox_processed=outbox_processed,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete expired login attempts and stale sessions."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--expired-retention-days",
        type=int,
        default=DEFAULT_EXPIRED_RETENTION_DAYS,
        help="keep sessions this many days after they expire (default %(default)s)",
    )
    parser.add_argument(
        "--revoked-retention-days",
        type=int,
        default=DEFAULT_REVOKED_RETENTION_DAYS,
        help="keep revoked sessions this many days (default %(default)s)",
    )
    parser.add_argument(
        "--outbox-retention-days",
        type=int,
        default=None,
        help="keep processed outbox rows this many days (default: OUTBOX_PROCESSED_RETENTION_DAYS)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> CleanupResult:
    database_url, configured_outbox_days = load_cleanup_settings()
    engine = create_engine_from_url(database_url)
    try:
        return await cleanup_auth(
            create_sessionmaker(engine),
            batch_size=args.batch_size,
            expired_retention_days=args.expired_retention_days,
            revoked_retention_days=args.revoked_retention_days,
            outbox_retention_days=(
                configured_outbox_days
                if args.outbox_retention_days is None
                else args.outbox_retention_days
            ),
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"deleted {result.login_attempts} login attempts and {result.sessions} sessions; "
        f"deleted {result.message_write_buckets} message write buckets; "
        f"deleted {result.outbox_processed} processed outbox rows"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
