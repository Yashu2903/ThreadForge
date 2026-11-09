"""Move legacy documents into live collaboration NOW instead of when someone first opens them.

    python -m app.commands.migrate_documents_to_collab [--dry-run] [--document UUID] [--room UUID]
                                                       [--limit N] [--service-url URL]

A `legacy` document keeps its text in `documents.content` (the old format). The collaboration service
turns it into a Yjs document the first time somebody opens it. This command triggers that for every
legacy document in advance, ONE AT A TIME, by asking the RUNNING collaboration service
(`POST /internal/migrate`). It does not build Yjs itself: there is exactly one seeding implementation
(the service's), and the service holds the single-writer lease, so nothing else may write Yjs state.

For each document the service

  1. skips it if it is already migrated or is `needs_repair`;
  2. builds the seed (the same `adapt` + `validateDocument` every snapshot is held to). A seed that
     cannot be built is reported as `invalid` with a reason (a code and a path, never content) and the
     document STAYS `legacy`: nothing is written, it is not marked `needs_repair`;
  3. (unless --dry-run) opens the document internally, which stores the Yjs state, marks it `ready` and
     writes the seed snapshot, then closes it again.

Per-document results: migrated, would_migrate (dry run), already_migrated, skipped_needs_repair,
invalid, failed. Safe to run again at any time (idempotent): migrated documents are no longer legacy.
`documents.content` is never modified. Exit status: 0 if nothing failed, 1 if any document was
`invalid` or `failed`, 2 if the service cannot be reached or the settings are missing.

Needs DATABASE_URL, INTERNAL_API_SECRET (the same value as the service's) and the service's address
(COLLAB_SERVICE_URL, default http://127.0.0.1:4010).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections import Counter

import httpx2
from sqlalchemy import text

from app.config import ConfigError, load_migrate_settings
from app.db import create_engine_from_url, create_sessionmaker

_LEGACY = text(
    """
    SELECT d.id FROM documents d
    LEFT JOIN document_yjs_state y ON y.document_id = d.id
    WHERE d.collab_state = 'legacy' AND y.document_id IS NULL
      AND (CAST(:doc AS uuid) IS NULL OR d.id = CAST(:doc AS uuid))
      AND (CAST(:room AS uuid) IS NULL OR d.room_id = CAST(:room AS uuid))
    ORDER BY d.created_at, d.id
    LIMIT :limit
    """
)
FAILED = {"invalid", "failed"}


class ServiceUnreachable(Exception):
    pass


async def migrate_one(
    client: httpx2.AsyncClient, url: str, secret: str, document_id: uuid.UUID, dry_run: bool
) -> tuple[str, str]:
    try:
        response = await client.post(
            f"{url}/internal/migrate",
            json={"document_id": str(document_id), "dry_run": dry_run},
            headers={"X-Internal-Secret": secret},
        )
    except httpx2.HTTPError:
        raise ServiceUnreachable from None
    if response.status_code == 401:
        return "failed", "service refused the secret (INTERNAL_API_SECRET differs)"
    if response.status_code != 200:
        return "failed", f"service answered {response.status_code}"
    body = response.json()
    return str(body.get("result", "failed")), str(body.get("detail", ""))


async def run(args: argparse.Namespace) -> int:
    settings = load_migrate_settings()
    url = (args.service_url or settings.collab_service_url).rstrip("/")
    engine = create_engine_from_url(settings.database_url.get_secret_value())
    try:
        async with create_sessionmaker(engine)() as db:
            ids = (
                (
                    await db.execute(
                        _LEGACY,
                        {"doc": args.document, "room": args.room, "limit": args.limit},
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    mode = "DRY RUN: nothing is changed" if args.dry_run else "migrating"
    print(f"{mode}; {len(ids)} legacy document(s) to look at; service {url}")
    counts: Counter[str] = Counter()
    async with httpx2.AsyncClient(timeout=60.0) as client:
        for document_id in ids:
            try:
                result, detail = await migrate_one(
                    client,
                    url,
                    settings.internal_api_secret.get_secret_value(),
                    document_id,
                    args.dry_run,
                )
            except ServiceUnreachable:
                print(
                    f"error: the collaboration service at {url} is not reachable", file=sys.stderr
                )
                return 2
            counts[result] += 1
            print(f"{document_id}  {result}{'  ' + detail if detail else ''}")
    print(
        "summary: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do")
    )
    return 1 if any(counts[k] for k in FAILED) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="validate every seed, change nothing"
    )
    parser.add_argument("--document", type=uuid.UUID, help="only this document")
    parser.add_argument("--room", type=uuid.UUID, help="only documents of this room")
    parser.add_argument("--limit", type=int, default=100_000)
    parser.add_argument(
        "--service-url", help="default: COLLAB_SERVICE_URL or http://127.0.0.1:4010"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
