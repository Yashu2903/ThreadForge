"""Logging setup. Logs carry the request ID; they must never carry secrets or query strings."""

from __future__ import annotations

import logging
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

APP_LOGGER = "threadforge"


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: str) -> None:
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(level.upper())
    if not any(getattr(h, "_threadforge", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler._threadforge = True  # type: ignore[attr-defined]
        handler.addFilter(RequestIDFilter())
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
        )
        logger.addHandler(handler)
    # HTTP client libraries log full request URLs; keep them quiet so a URL can never leak.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
