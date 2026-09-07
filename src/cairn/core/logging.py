"""Structured logging.

JSON to stdout, with request context carried automatically via contextvars so
every line inside a request or task is correlatable without threading a logger
through call signatures.

The redaction processor is a safety net, not a licence to be careless
(NFR-SEC-03). Never pass a secret to a log call in the first place.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import EventDict, WrappedLogger

__all__ = [
    "REDACTED",
    "bind_request_context",
    "clear_request_context",
    "configure_logging",
    "get_logger",
]

REDACTED = "[redacted]"

#: Substrings that mark a field as sensitive. Matched case-insensitively against
#: the key, so ``provider_api_key`` and ``X-Api-Key`` are both caught.
_SENSITIVE_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "credential",
    "private_key",
    "master_key",
    "session_id",
    "cookie",
)


def _is_sensitive(key: str) -> bool:
    # Normalise separators first: HTTP headers arrive as `X-Api-Key` while
    # Python fields are `api_key`. Matching only one spelling means the other
    # sails straight into the logs.
    normalised = key.lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalised for marker in _SENSITIVE_MARKERS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if _is_sensitive(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(item) for item in value)
    return value


def redaction_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Drop values whose key looks sensitive, at any nesting depth."""
    for key in list(event_dict.keys()):
        if _is_sensitive(str(key)):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    """Install the structlog pipeline. Call once, at process start."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy) through the same sink so the
    # output stream stays uniformly parseable.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).handlers.clear()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_request_context(**kwargs: Any) -> None:
    """Bind values onto every subsequent log line in this task/request."""
    bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


def clear_request_context() -> None:
    clear_contextvars()
