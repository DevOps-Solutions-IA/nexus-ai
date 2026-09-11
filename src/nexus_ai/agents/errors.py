"""Stable AI Agent Runtime error taxonomy (NXS-P13: NXS-AGENT-001, extends NXS-ERROR-001).

Every failure maps to exactly one stable ``NXS_AGENT_*`` code with an HTTP status, a safe
title and a ``retryable`` classification, rendered to RFC 9457 Problem Details. No error
ever carries a model provider API key, a raw provider response body, an Authorization
header, a raw prompt or any model reasoning — an adversary learns only the stable failure
class.
"""

from __future__ import annotations

from nexus_ai.core.errors import NxsError


class AgentNotFoundError(NxsError):
    code = "NXS_AGENT_NOT_FOUND"
    status = 404
    title = "Agent Not Found"


class AgentModelProviderAccountNotFoundError(NxsError):
    code = "NXS_AGENT_MODEL_ACCOUNT_NOT_FOUND"
    status = 404
    title = "Model Provider Account Not Found"


class AgentModelProfileNotFoundError(NxsError):
    code = "NXS_AGENT_MODEL_PROFILE_NOT_FOUND"
    status = 404
    title = "Model Profile Not Found"


class AgentSessionNotFoundError(NxsError):
    code = "NXS_AGENT_SESSION_NOT_FOUND"
    status = 404
    title = "Agent Session Not Found"


class AgentTurnNotFoundError(NxsError):
    code = "NXS_AGENT_TURN_NOT_FOUND"
    status = 404
    title = "Agent Turn Not Found"


class AgentConfigInvalidError(NxsError):
    code = "NXS_AGENT_CONFIG_INVALID"
    status = 422
    title = "Invalid Agent Configuration"


class AgentContextInvalidError(NxsError):
    """Deterministic context assembly failed — a referenced conversation / customer /
    voice session could not be resolved in this Organization, or the bounded context
    could not be built."""

    code = "NXS_AGENT_CONTEXT_INVALID"
    status = 409
    title = "Agent Context Invalid"


class AgentInvalidStateError(NxsError):
    """A requested agent-session or turn transition is not legal from the current state,
    or would mutate a terminal session."""

    code = "NXS_AGENT_INVALID_STATE"
    status = 409
    title = "Invalid Agent Session State"


class AgentBusyError(NxsError):
    """The agent session already has a turn in flight — a second concurrent turn is
    rejected deterministically (one turn per session at a time)."""

    code = "NXS_AGENT_BUSY"
    status = 409
    title = "Agent Session Busy"


class AgentProviderError(NxsError):
    """The model provider rejected the request or returned an error. Only a safe provider
    code (when present) is carried in ``extensions.provider_code`` — never its body."""

    code = "NXS_AGENT_PROVIDER_ERROR"
    status = 502
    title = "Model Provider Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        provider_code: str | None = None,
        provider_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail or self.title)
        self.retryable = retryable
        if provider_code is not None:
            self.extensions["provider_code"] = provider_code
        if provider_status is not None:
            self.extensions["provider_status"] = provider_status


class AgentProviderTimeoutError(NxsError):
    """A single model provider CALL timed out. When the provider may have already begun
    driving tool output this is AMBIGUOUS — the request is NOT blindly replayed."""

    code = "NXS_AGENT_PROVIDER_TIMEOUT"
    status = 504
    title = "Model Provider Timeout"
    retryable = False


class AgentTurnTimeoutError(NxsError):
    """The WHOLE agent turn exceeded its execution deadline (model calls + tool loop +
    continuation combined), bounded by ``min(agent.timeout_seconds, turn_deadline)``.
    Distinct from a single provider-call timeout. The in-flight task is cancelled and no
    stale model response is committed; the turn is persisted FAILED and the session stays
    usable for a fresh turn."""

    code = "NXS_AGENT_TURN_TIMEOUT"
    status = 504
    title = "Agent Turn Timeout"
    retryable = False


class AgentSessionExpiredError(NxsError):
    """A new turn was refused because the session reached its absolute lifetime ceiling
    (``started_at + max_session_seconds``). The session is terminalised EXPIRED before
    any model or Tool Engine call begins; it cannot be resurrected."""

    code = "NXS_AGENT_SESSION_EXPIRED"
    status = 409
    title = "Agent Session Expired"


class AgentOutputInvalidError(NxsError):
    """The model returned output that violates the provider-neutral contract (oversized
    content, a malformed / too-deep tool-argument object, a duplicate tool-call id, an
    unknown finish reason)."""

    code = "NXS_AGENT_OUTPUT_INVALID"
    status = 502
    title = "Model Output Invalid"


class AgentToolInvalidError(NxsError):
    """A model-requested tool call is structurally invalid — an out-of-bounds name,
    unparseable arguments, or a nesting depth beyond the configured limit."""

    code = "NXS_AGENT_TOOL_INVALID"
    status = 422
    title = "Agent Tool Request Invalid"


class AgentToolDeniedError(NxsError):
    """A model-requested tool is not on the agent's server-side allow-list, or the Tool
    Engine denied it (RBAC / policy / risk). The model cannot widen its own authority."""

    code = "NXS_AGENT_TOOL_DENIED"
    status = 403
    title = "Agent Tool Denied"


class AgentToolLoopLimitError(NxsError):
    """The bounded tool loop reached ``max_tool_iterations`` without a final model
    answer — the turn ends deterministically rather than looping forever."""

    code = "NXS_AGENT_TOOL_LOOP_LIMIT"
    status = 409
    title = "Agent Tool Loop Limit Reached"


class AgentIdempotencyConflictError(NxsError):
    code = "NXS_AGENT_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Agent Idempotency Conflict"


class AgentIdempotentReplayError(NxsError):
    """The idempotency key already resolved to a TERMINALLY-FAILED / CANCELLED turn. An
    idempotency key names ONE immutable logical turn — the same key never starts a fresh
    execution. The original terminal ``error_code`` is carried in
    ``extensions.original_error_code`` (and ``extensions.turn_state``) for the caller. A
    different attempt needs a different key."""

    code = "NXS_AGENT_IDEMPOTENT_REPLAY"
    status = 409
    title = "Agent Idempotent Replay"
    retryable = False

    def __init__(
        self, detail: str | None = None, *, original_error_code: str | None = None, turn_state: str
    ) -> None:
        super().__init__(detail or self.title)
        self.extensions["turn_state"] = turn_state
        if original_error_code is not None:
            self.extensions["original_error_code"] = original_error_code


class AgentCancelledError(NxsError):
    """The agent turn / session was cancelled (client cancel or voice barge-in). No
    stale model response is delivered."""

    code = "NXS_AGENT_CANCELLED"
    status = 409
    title = "Agent Execution Cancelled"


class AgentNotAuthorizedError(NxsError):
    """A referenced model profile / conversation / customer / voice session is not owned
    by the authenticated Organization."""

    code = "NXS_AGENT_NOT_AUTHORIZED"
    status = 403
    title = "Agent Not Authorized"


class AgentDisabledError(NxsError):
    code = "NXS_AGENT_DISABLED"
    status = 409
    title = "Agent Runtime Disabled"


#: Every agent error, for the Problem Details contract and the contract tests.
AGENT_ERRORS: tuple[type[NxsError], ...] = (
    AgentNotFoundError,
    AgentModelProviderAccountNotFoundError,
    AgentModelProfileNotFoundError,
    AgentSessionNotFoundError,
    AgentTurnNotFoundError,
    AgentConfigInvalidError,
    AgentContextInvalidError,
    AgentInvalidStateError,
    AgentBusyError,
    AgentProviderError,
    AgentProviderTimeoutError,
    AgentTurnTimeoutError,
    AgentSessionExpiredError,
    AgentOutputInvalidError,
    AgentToolInvalidError,
    AgentToolDeniedError,
    AgentToolLoopLimitError,
    AgentIdempotencyConflictError,
    AgentIdempotentReplayError,
    AgentCancelledError,
    AgentNotAuthorizedError,
    AgentDisabledError,
)


def provider_rest_failure(
    detail: str, *, status_code: int, provider_code: str | None = None
) -> NxsError:
    """Map a model provider REST status to the stable agent taxonomy (never its body)."""
    if status_code in (401, 403):
        return AgentNotAuthorizedError("the model provider rejected the credential")
    if status_code == 429:
        return AgentProviderError(
            "the model provider rate-limited the request",
            provider_code=provider_code,
            provider_status=status_code,
            retryable=True,
        )
    if status_code == 408 or status_code == 504:
        return AgentProviderTimeoutError("the model provider timed out")
    return AgentProviderError(
        detail,
        provider_code=provider_code,
        provider_status=status_code,
        retryable=status_code in (500, 502, 503),
    )
