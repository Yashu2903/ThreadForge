"""Inspect view of retrieval: exactly which passages a query would retrieve from chosen source versions.

    python -m app.commands.ai_retrieve --room ROOM_ID --user USER_ID --version VERSION_ID [--version ...] "query"

This is the SAME function the answer path uses (app/ai/retrieval.py), run with the same scope rules:
only the listed versions' current index, inside the room. The query is embedded with the provider key
the user would use (their own, or the server's), so it is one real, ledgered embedding call; that is
why this is an operator command and not an API route (provider calls happen in worker-class
processes only). It prints ids, locators, ranks and a short excerpt of each passage, so it is an
operator tool: do not paste its output where room content must not go.

Exit codes: 0 ok, 2 bad configuration or arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import text

from app.ai import keys
from app.ai.analysis.gateway import AnalysisAbort, AnalysisGateway
from app.ai.retrieval import retrieve
from app.config import ConfigError, load_settings
from app.db import create_engine, create_sessionmaker


class _PreviewJob:
    """Just enough of an analysis job for the gateway: whose key, which room, a tiny call cap."""

    def __init__(self, user_id: uuid.UUID, room_id: uuid.UUID, source: str) -> None:
        self.id = None
        self.requested_by = user_id
        self.charged_user_id = user_id
        self.room_id = room_id
        self.key_source_generation = source
        self.key_source_embedding = source
        self.caps = {"max_provider_calls": 2, "max_images": 0}


async def preview(
    room_id: uuid.UUID, user_id: uuid.UUID, versions: list[uuid.UUID], query: str
) -> int:
    settings = load_settings()
    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    try:
        async with sm() as db:
            choice = await keys.choose_key_source(db, settings, user_id, "google")
            gens = (
                (
                    await db.execute(
                        text(
                            "SELECT current_generation_id FROM source_versions "
                            "WHERE room_id = :r AND id = ANY(:v) AND current_generation_id IS NOT NULL"
                        ),
                        {"r": room_id, "v": versions},
                    )
                )
                .scalars()
                .all()
            )
        if choice.source is None:
            print(f"error: no usable embedding key ({choice.problem})", file=sys.stderr)
            return 2

        async def never() -> bool:
            return False

        gateway = AnalysisGateway(
            sm, settings, _PreviewJob(user_id, room_id, choice.source), is_cancelled=never
        )
        try:
            vector = (await gateway.embed_query(query))[0]
        except AnalysisAbort as abort:
            print(f"error: embedding failed ({abort.code})", file=sys.stderr)
            return 2
        finally:
            await gateway.aclose()
        async with sm() as db:
            result = await retrieve(
                db,
                settings,
                room_id=room_id,
                generation_ids=list(gens),
                query=query,
                query_vector=vector,
            )
    finally:
        await engine.dispose()
    print(
        f"searched {len(result.searched_generations)} index(es); {result.considered} candidates; "
        f"{len(result.passages)} selected; {result.dropped_for_budget} dropped for the budget"
    )
    if result.empty:
        print("nothing relevant was found in the selected sources")
    for p in result.passages:
        loc = p.locator
        where = f"p{loc.get('page_start')}" if loc.get("page_start") else "-"
        print(
            f"- {p.id} v={p.source_version_id} #{p.ordinal} {p.kind} {where} "
            f"lex={p.lexical_rank} vec={p.vector_rank} score={p.score:.4f} {p.text[:100]!r}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--room", required=True, type=uuid.UUID)
    parser.add_argument("--user", required=True, type=uuid.UUID)
    parser.add_argument("--version", required=True, action="append", type=uuid.UUID)
    parser.add_argument("query")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(preview(args.room, args.user, args.version, args.query))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
