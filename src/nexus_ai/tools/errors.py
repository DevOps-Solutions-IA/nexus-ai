"""Stable Tool Engine error taxonomy (NXS-TOOL-001, extends NXS-ERROR-001).

Every failure the Tool Engine can produce maps to exactly one stable ``NXS_TOOL_*`` code
with an HTTP status, a safe human title and a ``retryable`` classification, rendered to
RFC 9457 Problem Details through :mod:`nexus_ai.core.problem_details`. Downstream
Integration Hub failures are wrapped as ``NXS_TOOL_DOWNSTREAM_ERROR`` with the original
``NXS_INT_*`` code carried in bounded ``extensions`` — the Tool Engine never re-exports
the Integration Hub's transport detail directly.
"""

from __future__ import annotations

from typing import Any

from nexus_ai.core.errors import NxsError


class ToolNotFoundError(NxsError):
    code = "NXS_TOOL_NOT_FOUND"
    status = 404
    title = "Tool Not Found"


class ToolConflictError(NxsError):
    code = "NXS_TOOL_CONFLICT"
    status = 409
    title = "Tool Conflict"


class ToolDisabledError(NxsError):
    code = "NXS_TOOL_DISABLED"
    status = 409
    title = "Tool Not Active"


class ToolConfigInvalidError(NxsError):
    code = "NXS_TOOL_CONFIG_INVALID"
    status = 422
    title = "Invalid Tool Definition"


class ToolBindingInvalidError(NxsError):
    """The tool's bound integration or operation is missing, disabled or mismatched."""

    code = "NXS_TOOL_BINDING_INVALID"
    status = 409
    title = "Invalid Tool Binding"


class ToolArgumentsInvalidError(NxsError):
    """The invocation arguments failed the tool's input JSON Schema, or carried an
    argument the schema does not permit."""

    code = "NXS_TOOL_ARGS_INVALID"
    status = 422
    title = "Invalid Tool Arguments"

    def __init__(
        self, detail: str | None = None, *, errors: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(detail or self.title)
        if errors is not None:
            self.extensions["errors"] = errors


class ToolPermissionDeniedError(NxsError):
    """The caller principal lacks a permission the tool requires, in this Organization.
    Server-side only — a model-supplied permission claim is never trusted."""

    code = "NXS_TOOL_PERMISSION_DENIED"
    status = 403
    title = "Tool Permission Denied"


class ToolPolicyDeniedError(NxsError):
    """The Organization or tool policy forbids this invocation (e.g. a risk ceiling)."""

    code = "NXS_TOOL_POLICY_DENIED"
    status = 403
    title = "Tool Policy Denied"


class ToolIdempotencyRequiredError(NxsError):
    """A side-effecting tool with a REQUIRED idempotency policy was invoked without an
    idempotency key."""

    code = "NXS_TOOL_IDEMPOTENCY_REQUIRED"
    status = 422
    title = "Tool Idempotency Key Required"


class ToolIdempotencyConflictError(NxsError):
    """The same idempotency key was replayed with a different request fingerprint."""

    code = "NXS_TOOL_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Tool Idempotency Conflict"


class ToolExecutionInProgressError(NxsError):
    """An invocation with this idempotency key is still running elsewhere."""

    code = "NXS_TOOL_EXECUTION_IN_PROGRESS"
    status = 409
    title = "Tool Execution In Progress"
    retryable = True


class ToolDownstreamError(NxsError):
    """The governed Integration Hub execution failed. The original ``NXS_INT_*`` code and
    upstream status are carried in ``extensions`` (bounded, no bodies / headers)."""

    code = "NXS_TOOL_DOWNSTREAM_ERROR"
    status = 502
    title = "Tool Downstream Execution Failed"

    def __init__(
        self,
        detail: str | None = None,
        *,
        downstream_code: str | None = None,
        upstream_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail or self.title)
        self.retryable = retryable
        if downstream_code is not None:
            self.extensions["downstream_code"] = downstream_code
        if upstream_status is not None:
            self.extensions["upstream_status"] = upstream_status


class ToolResultInvalidError(NxsError):
    """The downstream result failed the tool's output JSON Schema — an untrusted external
    response is never handed to the caller half-validated."""

    code = "NXS_TOOL_RESULT_INVALID"
    status = 502
    title = "Invalid Tool Result"


class ToolTimeoutError(NxsError):
    code = "NXS_TOOL_TIMEOUT"
    status = 504
    title = "Tool Execution Timed Out"
    retryable = True


class ToolRateLimitedError(NxsError):
    code = "NXS_TOOL_RATE_LIMITED"
    status = 429
    title = "Tool Rate Limited"
    retryable = True


class ToolExecutionFailedError(NxsError):
    """A generic terminal invocation failure that fits no more specific category."""

    code = "NXS_TOOL_EXECUTION_FAILED"
    status = 502
    title = "Tool Execution Failed"


#: Every Tool Engine error, for the public Problem Details contract and contract tests.
TOOL_ERRORS: tuple[type[NxsError], ...] = (
    ToolNotFoundError,
    ToolConflictError,
    ToolDisabledError,
    ToolConfigInvalidError,
    ToolBindingInvalidError,
    ToolArgumentsInvalidError,
    ToolPermissionDeniedError,
    ToolPolicyDeniedError,
    ToolIdempotencyRequiredError,
    ToolIdempotencyConflictError,
    ToolExecutionInProgressError,
    ToolDownstreamError,
    ToolResultInvalidError,
    ToolTimeoutError,
    ToolRateLimitedError,
    ToolExecutionFailedError,
)
