"""Run the provider self-check now and store the result (what the AI worker does at start).

    python -m app.commands.ai_selfcheck

Makes a handful of small REAL provider calls with the SERVER keys (a fraction of a cent) and needs
the full application settings (models, keys). Exit status 0 only if every required capability
passed. Output: capability, ok/failed, safe code.
"""

from __future__ import annotations

import asyncio
import sys

from app.ai.selfcheck import run_selfcheck
from app.ai.status import REQUIRED
from app.config import ConfigError, load_settings
from app.db import create_engine, create_sessionmaker


async def _run() -> int:
    settings = load_settings()
    if not settings.ai_enabled:
        print("AI_ENABLED is false: nothing to check.", file=sys.stderr)
        return 2
    engine = create_engine(settings)
    try:
        results = await run_selfcheck(create_sessionmaker(engine), settings)
    finally:
        await engine.dispose()
    for name, result in results.items():
        print(f"{name:18} {'ok' if result.ok else 'FAILED':7} {result.code} {result.detail}")
    missing = [n for n in REQUIRED if n not in results]
    if missing:
        print("not checked (no server key): " + ", ".join(missing))
    return 0 if not missing and all(results[n].ok for n in REQUIRED) else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
