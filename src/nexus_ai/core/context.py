"""Request-scoped context using :mod:`contextvars` (NXS-CTX-001).

The context is isolated per asyncio task, so concurrent requests never observe each
other's identifiers. Future domain fields (organization_id, actor_id, conversation_id)
are intentionally absent until those domains exist.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from nexus_ai.core.identifiers import new_id


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    correlation_id: str
    trace_id: str | None = None

    def as_log_fields(self) -> dict[str, str]:
        fields: dict[str, str] = {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
        }
        if self.trace_id is not None:
            fields["trace_id"] = self.trace_id
        return fields


_current: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "nexus_ai_request_context", default=None
)


def current_context() -> RequestContext | None:
    return _current.get()


def bind_context(context: RequestContext) -> contextvars.Token[RequestContext | None]:
    return _current.set(context)


def reset_context(token: contextvars.Token[RequestContext | None]) -> None:
    _current.reset(token)


@contextmanager
def request_context(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
) -> Iterator[RequestContext]:
    resolved_request_id = request_id or new_id()
    context = RequestContext(
        request_id=resolved_request_id,
        correlation_id=correlation_id or resolved_request_id,
        trace_id=trace_id,
    )
    token = bind_context(context)
    try:
        yield context
    finally:
        reset_context(token)


def context_log_fields() -> dict[str, str]:
    context = _current.get()
    return {} if context is None else context.as_log_fields()
