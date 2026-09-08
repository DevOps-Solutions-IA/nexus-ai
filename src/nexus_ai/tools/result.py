"""Normalise an Integration Hub outcome (or failure) into a ToolResult (NXS-TOOL-001).

The Tool Engine never re-exports the Integration Hub's transport detail. A downstream
``NXS_INT_*`` failure is wrapped as ``NXS_TOOL_DOWNSTREAM_ERROR`` (or the matching
timeout / rate-limit tool code) with the original code carried in bounded ``extensions``.
An untrusted external response is validated against the tool's output schema before it
reaches the caller.
"""

from __future__ import annotations

from nexus_ai.core.errors import NxsError
from nexus_ai.tools.entities import ToolResultClass
from nexus_ai.tools.errors import (
    ToolDownstreamError,
    ToolRateLimitedError,
    ToolTimeoutError,
)

_TIMEOUT_CODES = frozenset({"NXS_INT_TIMEOUT"})
_RATE_LIMIT_CODES = frozenset({"NXS_INT_UPSTREAM_RATE_LIMITED", "NXS_INT_OUTBOUND_RATE_LIMITED"})

_RESULT_CLASS_FOR_TOOL_CODE: dict[str, ToolResultClass] = {
    "NXS_TOOL_NOT_FOUND": ToolResultClass.NOT_FOUND,
    "NXS_TOOL_DISABLED": ToolResultClass.DISABLED,
    "NXS_TOOL_ARGS_INVALID": ToolResultClass.ARGS_INVALID,
    "NXS_TOOL_PERMISSION_DENIED": ToolResultClass.PERMISSION_DENIED,
    "NXS_TOOL_POLICY_DENIED": ToolResultClass.POLICY_DENIED,
    "NXS_TOOL_BINDING_INVALID": ToolResultClass.BINDING_INVALID,
    "NXS_TOOL_IDEMPOTENCY_REQUIRED": ToolResultClass.IDEMPOTENCY_REQUIRED,
    "NXS_TOOL_IDEMPOTENCY_CONFLICT": ToolResultClass.IDEMPOTENCY_CONFLICT,
    "NXS_TOOL_DOWNSTREAM_ERROR": ToolResultClass.DOWNSTREAM_ERROR,
    "NXS_TOOL_RESULT_INVALID": ToolResultClass.RESULT_INVALID,
    "NXS_TOOL_TIMEOUT": ToolResultClass.TIMEOUT,
    "NXS_TOOL_RATE_LIMITED": ToolResultClass.RATE_LIMITED,
    "NXS_TOOL_EXECUTION_FAILED": ToolResultClass.EXECUTION_FAILED,
}


def map_downstream_error(exc: NxsError) -> NxsError:
    """Translate an Integration Hub ``NxsError`` into a Tool Engine error."""
    code = getattr(exc, "code", "")
    if code in _TIMEOUT_CODES:
        return ToolTimeoutError(
            "the downstream integration timed out",
            extensions={"timeout_scope": "integration", "downstream_code": code},
        )
    if code in _RATE_LIMIT_CODES:
        return ToolRateLimitedError("the downstream integration was rate limited")
    upstream = exc.extensions.get("upstream_status")
    return ToolDownstreamError(
        "the governed integration execution failed",
        downstream_code=code or None,
        upstream_status=int(upstream) if isinstance(upstream, int) else None,
        retryable=bool(getattr(exc, "retryable", False)),
    )


def tool_result_class_for(error_code: str) -> ToolResultClass:
    return _RESULT_CLASS_FOR_TOOL_CODE.get(error_code, ToolResultClass.EXECUTION_FAILED)
