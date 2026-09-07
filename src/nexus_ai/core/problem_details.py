"""RFC 9457 Problem Details rendering (NXS-ERROR-001).

Every error response — application errors, validation errors, 404, 405 and unexpected
exceptions — is normalised to this shape. Stack traces and internal identifiers are
never included.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai.core.context import current_context
from nexus_ai.core.errors import ERROR_DOC_BASE, InternalError, NxsError

PROBLEM_MEDIA_TYPE = "application/problem+json"
_MAX_DETAIL = 2048


class ProblemDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    type: str = "about:blank"
    title: str
    status: int = Field(ge=100, le=599)
    detail: str
    instance: str | None = None
    code: str
    request_id: str | None = None


_RESERVED_MEMBERS = frozenset(
    {"type", "title", "status", "detail", "instance", "code", "request_id"}
)


def _truncate(text: str, limit: int = _MAX_DETAIL) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_extensions(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key not in _RESERVED_MEMBERS}


def from_error(error: NxsError, *, instance: str | None = None) -> ProblemDetails:
    context = current_context()
    extra: dict[str, Any] = _safe_extensions(error.extensions)
    if error.retryable:
        extra.setdefault("retryable", True)
    return ProblemDetails(
        type=error.type_uri,
        title=error.title,
        status=error.status,
        detail=_truncate(error.detail),
        instance=instance,
        code=error.code,
        request_id=None if context is None else context.request_id,
        **extra,
    )


def generic(
    *,
    status: int,
    title: str,
    detail: str,
    code: str,
    instance: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ProblemDetails:
    context = current_context()
    return ProblemDetails(
        type=f"{ERROR_DOC_BASE}{code}",
        title=title,
        status=status,
        detail=_truncate(detail),
        instance=instance,
        code=code,
        request_id=None if context is None else context.request_id,
        **_safe_extensions(extra or {}),
    )


def unexpected(instance: str | None = None) -> ProblemDetails:
    """Safe body for an unhandled exception — no diagnostic leakage."""
    return from_error(InternalError("An unexpected error occurred."), instance=instance)
