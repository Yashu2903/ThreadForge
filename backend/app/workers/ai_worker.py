"""The AI worker process:  python -m app.workers.ai_worker

What it does (the only process that calls AI providers):
  * the provider SELF-CHECK at start and every AI_SELFCHECK_INTERVAL_SECONDS (results for GET /ai/status);
  * the WORK loop, AI_WORKER_CONCURRENCY at a time: it advances requests whose files became ready,
    expires stale ones, runs the next queued `@AI` request (app/ai/answer/execute.py) and otherwise the
    next analysis job (app/ai/analysis/pipeline.py), and sweeps work whose worker died.

    python -m app.workers.ai_worker --once      # one self-check, then exit (exit 0 = required checks passed)
    python -m app.workers.ai_worker --drain     # self-check, run queued jobs until none are left, exit

The API and the worker must see the SAME storage directory (STORAGE_PATH).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.analysis import jobs
from app.ai.analysis.pipeline import process_next
from app.ai.answer.execute import process_next_request
from app.ai.selfcheck import run_selfcheck
from app.ai.status import REQUIRED
from app.config import Settings, load_settings
from app.db import create_engine, create_sessionmaker
from app.logging_config import APP_LOGGER, configure_logging
from app.services import ai_requests
from app.storage import Storage

log = logging.getLogger(f"{APP_LOGGER}.ai.worker")


async def selfcheck_once(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    results = await run_selfcheck(sessionmaker, settings)
    failed = [n for n in REQUIRED if n in results and not results[n].ok]
    log.info(
        "selfcheck done ok=%s failed_required=%s",
        sorted(n for n, r in results.items() if r.ok),
        failed,
    )
    return 1 if failed else 0


async def analysis_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    stop: asyncio.Event,
    *,
    drain: bool = False,
) -> None:
    """One claim loop. Several of these run side by side (AI_WORKER_CONCURRENCY)."""
    while not stop.is_set():
        try:
            await jobs.sweep_abandoned(sessionmaker)
            # requests: move waiting ones forward, expire the stale, then run the next queued one;
            # an answer is what a person is waiting for, so requests go before new analysis
            await ai_requests.advance_waiting(sessionmaker, settings)
            await ai_requests.expire_stale(sessionmaker, settings)
            await ai_requests.sweep_abandoned(sessionmaker, settings)
            outcome = await process_next_request(sessionmaker, settings, storage)
            if outcome is None:
                outcome = await process_next(sessionmaker, settings, storage)
        except Exception:
            log.exception("action=analysis_loop_error")
            outcome = None
        if outcome is not None:
            continue
        if drain:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.ai_worker_poll_seconds)


async def periodic_selfcheck(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await selfcheck_once(sessionmaker, settings)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.ai_selfcheck_interval_seconds)


async def main_async(once: bool, drain: bool) -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.ai_enabled:
        log.error("AI_ENABLED is false; the AI worker has nothing to do")
        return 2
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    storage = Storage(settings.storage_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows event loops
            loop.add_signal_handler(sig, stop.set)
    code = 0
    try:
        if once:
            code = await selfcheck_once(sessionmaker, settings)
        elif drain:
            code = await selfcheck_once(sessionmaker, settings)
            await asyncio.gather(
                *(
                    analysis_loop(sessionmaker, settings, storage, stop, drain=True)
                    for _ in range(settings.ai_worker_concurrency)
                )
            )
        else:
            await asyncio.gather(
                periodic_selfcheck(sessionmaker, settings, stop),
                *(
                    analysis_loop(sessionmaker, settings, storage, stop)
                    for _ in range(settings.ai_worker_concurrency)
                ),
            )
    finally:
        await engine.dispose()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args.once, args.drain))


if __name__ == "__main__":
    raise SystemExit(main())
