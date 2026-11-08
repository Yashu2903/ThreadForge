"""Operator repair for a document in `needs_repair`: restore it from a validated snapshot.

    python -m app.commands.restore_document --document <uuid> --backup-dir ./backups
    python -m app.commands.restore_document --document <uuid> --snapshot <uuid> --backup-dir ./b

What it does:
  1. saves the raw Yjs state to <backup-dir>/doc-<id>-counter-<n>.ystate FIRST (nothing a person
     typed is lost, even the part that made the document invalid), unless it has no state;
  2. in ONE transaction: refuses to run while a collaboration-service instance holds the lease
     (STOP THE SERVICE FIRST: the Yjs state is about to be deleted), copies the chosen snapshot
     (default: the newest) into a new 'restore' snapshot, deletes the Yjs state, and sets
     collab_state = 'legacy'.
The next time anyone opens the document the collaboration service seeds a fresh Yjs document from
the restore snapshot (services/collab.get_seed). documents.content is never touched.

Needs only DATABASE_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

from app.config import ConfigError, load_database_url
from app.db import create_engine_from_url, create_sessionmaker
from app.errors import AppError
from app.services import collab


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--document", required=True, type=uuid.UUID)
    parser.add_argument("--snapshot", type=uuid.UUID, help="default: the newest snapshot")
    parser.add_argument("--backup-dir", type=Path, help="where the raw Yjs state is saved first")
    parser.add_argument(
        "--no-backup", action="store_true", help="do not save the raw Yjs state (not recommended)"
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> str:
    engine = create_engine_from_url(load_database_url())
    sessionmaker = create_sessionmaker(engine)
    try:
        if not args.no_backup and args.backup_dir is None:
            raise SystemExit("error: pass --backup-dir (or --no-backup)")
        # Back up FIRST: if writing the file fails nothing has been deleted yet.
        stored = await collab.load_state(sessionmaker, args.document)
        note = "no Yjs state existed"
        if stored is not None and stored.update_counter >= 0:
            note = "raw Yjs state discarded (--no-backup)"
            if not args.no_backup:
                args.backup_dir.mkdir(parents=True, exist_ok=True)
                path = (
                    args.backup_dir / f"doc-{args.document}-counter-{stored.update_counter}.ystate"
                )
                path.write_bytes(stored.state)
                note = f"raw Yjs state saved to {path}"
        await collab.restore_document(sessionmaker, args.document, args.snapshot)
        return note
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        note = asyncio.run(_run(args))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AppError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(f"restored {args.document}: {note}; it will be re-seeded on next open")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
