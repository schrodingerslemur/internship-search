"""Structured logging configuration."""

from __future__ import annotations

import logging
import sys

import structlog

_configured = False


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level, 20))
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, 20)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "app") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
