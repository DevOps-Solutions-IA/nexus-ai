"""Structured, correlated, redacted logging (NXS-LOG-001).

Production emits single-line JSON with ``timestamp``, ``severity``, ``service``,
``environment``, ``event`` and request correlation. Sensitive keys and URL credentials
are redacted, field values are length-bounded, and request bodies / auth headers /
cookies are never logged by this subsystem. Standard-library loggers (uvicorn,
SQLAlchemy) are routed through the same renderer.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor

from nexus_ai.core.config import Settings
from nexus_ai.core.context import context_log_fields

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "cookie",
        "set-cookie",
        "session",
        "dsn",
        "database_url",
        "private_key",
    }
)
_REDACTED = "«redacted»"
_URL_CREDENTIALS = re.compile(r"://([^:/@\s]+):([^@/\s]+)@")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(r"://\1:«redacted»@", value)
    return value


def redact_processor(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def bound_fields_processor(max_length: int) -> Processor:
    def processor(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
        for key, value in list(event_dict.items()):
            if isinstance(value, str) and len(value) > max_length:
                event_dict[key] = value[: max_length - 1] + "…"
        return event_dict

    return processor


def context_processor(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    for key, value in context_log_fields().items():
        event_dict.setdefault(key, value)
    return event_dict


def service_processor(service: str, environment: str) -> Processor:
    def processor(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("environment", environment)
        return event_dict

    return processor


def rename_severity(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    if "level" in event_dict:
        event_dict["severity"] = str(event_dict.pop("level")).upper()
    return event_dict


def _shared_processors(settings: Settings) -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        context_processor,
        service_processor(settings.service_name, str(settings.environment)),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        rename_severity,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        redact_processor,
        bound_fields_processor(settings.logging.max_field_length),
    ]


def configure_logging(settings: Settings) -> None:
    level = logging.getLevelNamesMapping()[settings.logging.level]
    shared = _shared_processors(settings)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.logging.format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str = "nexus_ai") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
