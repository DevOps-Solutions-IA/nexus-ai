"""Event platform error taxonomy and failure classification (NXS-EVENT-007).

Every failure in the event pipeline is either RETRYABLE (transient — retry under bounded
backoff) or TERMINAL (poison — send to the dead-letter path, never hot-loop). The
classification is explicit: a handler raises :class:`HandlerRetryableError` or
:class:`HandlerTerminalError`, and anything else is classified conservatively by
:func:`classify_failure`.

Public codes follow the stable ``NXS_*`` contract even though P04 exposes no broad event
API — the codes appear in structured logs and dead-letter records.
"""

from __future__ import annotations

import enum

from nexus_ai.core.errors import NxsError


class FailureClass(enum.StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class EventPlatformError(NxsError):
    """Base for every event platform error."""

    code = "NXS_EVENT_INTERNAL"
    status = 500
    title = "Event Platform Error"
    failure_class: FailureClass = FailureClass.TERMINAL


class EventContractError(EventPlatformError):
    """The envelope or payload failed schema/version validation. Terminal — a malformed
    event will never become well-formed on redelivery."""

    code = "NXS_EVENT_CONTRACT_INVALID"
    status = 422
    title = "Event Contract Invalid"
    failure_class = FailureClass.TERMINAL


class UnknownEventTypeError(EventContractError):
    code = "NXS_EVENT_TYPE_UNKNOWN"
    title = "Unknown Event Type"


class UnsupportedEventVersionError(EventContractError):
    code = "NXS_EVENT_VERSION_UNSUPPORTED"
    title = "Unsupported Event Version"


class SubjectValidationError(EventPlatformError):
    """A NATS subject was malformed, injection-like or secret-bearing."""

    code = "NXS_EVENT_SUBJECT_INVALID"
    status = 400
    title = "Event Subject Invalid"
    failure_class = FailureClass.TERMINAL


class EventTenantScopeError(EventPlatformError):
    """A forged tenant identity, a cross-tenant aggregate id or scope confusion."""

    code = "NXS_EVENT_TENANT_SCOPE"
    status = 403
    title = "Event Tenant Scope Violation"
    failure_class = FailureClass.TERMINAL


class EventPublishError(EventPlatformError):
    """A transient failure while publishing to JetStream (no acknowledgement, timeout)."""

    code = "NXS_EVENT_PUBLISH_FAILED"
    status = 503
    title = "Event Publish Failed"
    retryable = True
    failure_class = FailureClass.RETRYABLE


class DurableTransportUnavailableError(EventPlatformError):
    """JetStream durability is required but unavailable. Fail closed in hardened envs."""

    code = "NXS_EVENT_DURABLE_TRANSPORT_UNAVAILABLE"
    status = 503
    title = "Durable Event Transport Unavailable"
    retryable = True
    failure_class = FailureClass.RETRYABLE


class HandlerRetryableError(EventPlatformError):
    """Raised by a consumer handler to request a bounded retry."""

    code = "NXS_EVENT_HANDLER_RETRYABLE"
    status = 503
    title = "Event Handler Retryable Failure"
    retryable = True
    failure_class = FailureClass.RETRYABLE


class HandlerTerminalError(EventPlatformError):
    """Raised by a consumer handler to send the message straight to the dead-letter path."""

    code = "NXS_EVENT_HANDLER_TERMINAL"
    status = 422
    title = "Event Handler Terminal Failure"
    failure_class = FailureClass.TERMINAL


# Transient infrastructure exception names that always mean "retry", even when they reach
# the classifier wrapped in something generic. Kept as names so we never import optional
# driver internals at module load.
_RETRYABLE_NAMES: frozenset[str] = frozenset(
    {
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "OSError",
        "BrokenPipeError",
        "NoRespondersError",
        "ServiceUnavailableError",
        "DisconnectedError",
        "OperationalError",
        "InterfaceError",
        "DBAPIError",
        "InternalError_",
    }
)


def classify_failure(exc: BaseException) -> FailureClass:
    """Classify an exception conservatively.

    Explicit event-platform classification wins. A known-transient infrastructure error
    is RETRYABLE. Everything else is TERMINAL so a genuine bug cannot silently spin
    forever — an operator promotes it to retryable after a fix.
    """
    if isinstance(exc, EventPlatformError):
        return exc.failure_class
    for klass in type(exc).__mro__:
        if klass.__name__ in _RETRYABLE_NAMES:
            return FailureClass.RETRYABLE
    return FailureClass.TERMINAL


def safe_error_code(exc: BaseException) -> str:
    """A short, safe identifier for an exception — never its message (may hold secrets)."""
    if isinstance(exc, NxsError):
        return exc.code
    return type(exc).__name__[:64]


def safe_error_summary(exc: BaseException) -> str:
    """A safe human descriptor for durable records: the stable title / type, NEVER the
    exception message (handler messages are developer-controlled and may hold secrets)."""
    if isinstance(exc, NxsError):
        return exc.title
    return type(exc).__name__[:120]
