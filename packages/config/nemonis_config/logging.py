"""Structured logging with mandatory redaction.

Every record passes through redaction before reaching any sink (security.md §3).
The processor is inserted first in the chain so nothing can bypass it by adding a
later processor.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from nemonis_config.redaction import redact_value


def _redaction_processor(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Redact every value in the record, including the message itself."""
    return {k: redact_value(str(k), v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog and stdlib logging. Idempotent."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Redaction runs after context merging (so contextvars are covered)
            # and before rendering (so nothing is serialised unredacted).
            _redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper(), force=True)
    for noisy in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
