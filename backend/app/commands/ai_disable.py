"""Global AI kill switch: stop AI everywhere, no redeploy.

    python -m app.commands.ai_disable [--note "why"]

Sets ai_global_flags.ai_enabled = false. Effect is immediate for new requests and for workers at their
next stage boundary (they re-read the flag). Chat, documents and everything else keep working.
Undo with `python -m app.commands.ai_enable`. Needs only DATABASE_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.ai.status import set_global_flag
from app.config import ConfigError, load_database_url
from app.db import create_engine_from_url, create_sessionmaker


async def _set(enabled: bool, note: str) -> None:
    engine = create_engine_from_url(load_database_url())
    try:
        async with create_sessionmaker(engine)() as db, db.begin():
            await set_global_flag(db, enabled, note)
    finally:
        await engine.dispose()


def run(enabled: bool, argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--note", default="", help="why (kept in ai_global_flags.note)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_set(enabled, args.note))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("AI is now " + ("ENABLED" if enabled else "DISABLED") + " (global switch)")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(False, argv)


if __name__ == "__main__":
    raise SystemExit(main())
