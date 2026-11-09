"""The AI worker process:  python -m app.workers.ai_worker

CHECKPOINT 1 SCOPE (stated plainly): this process runs the provider self-check at start and every
AI_SELFCHECK_INTERVAL_SECONDS, and records the result for GET /ai/status. It does NOT yet claim
analysis jobs or answer requests; those queues and their tables arrive in checkpoints 3-4, where the
claim loop is added to this same entry point.

    python -m app.workers.ai_worker --once      # one check, then exit (exit 0 = required checks passed)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from app.ai.selfcheck import run_selfcheck
from app.ai.status import REQUIRED
from app.config import load_settings
from app.db import create_engine, create_sessionmaker
from app.logging_config import APP_LOGGER, configure_logging

log = logging.getLogger(f"{APP_LOGGER}.ai.worker")


async def main_async(once: bool) -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.ai_enabled:
        log.error("AI_ENABLED is false; the AI worker has nothing to do")
        return 2
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows event loops
            loop.add_signal_handler(sig, stop.set)
    code = 0
    try:
        while not stop.is_set():
            results = await run_selfcheck(sessionmaker, settings)
            failed = [n for n in REQUIRED if n in results and not results[n].ok]
            log.info(
                "selfcheck done ok=%s failed_required=%s",
                sorted(n for n, r in results.items() if r.ok),
                failed,
            )
            code = 1 if failed else 0
            if once:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.ai_selfcheck_interval_seconds)
    finally:
        await engine.dispose()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true")
    return asyncio.run(main_async(parser.parse_args().once))


if __name__ == "__main__":
    raise SystemExit(main())
