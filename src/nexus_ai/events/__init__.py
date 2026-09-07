"""Data and Event Platform (NXS-P04).

The permanent enterprise event foundation: a canonical versioned envelope, a
deterministic tenant-safe subject taxonomy, a PostgreSQL transactional outbox, a
JetStream publisher that confirms every publication, a durable consumer framework with
explicit acknowledgement and bounded retry, durable consumer idempotency and an
explicit dead-letter path.

Delivery semantics: **at-least-once transport + idempotent consumption = effectively
once business effect.** Exactly-once transport is NOT claimed.
"""

from __future__ import annotations

from nexus_ai.events.envelope import EventEnvelope, EventScope
from nexus_ai.events.errors import (
    EventContractError,
    EventPlatformError,
    EventPublishError,
    EventTenantScopeError,
    FailureClass,
    HandlerRetryableError,
    HandlerTerminalError,
    SubjectValidationError,
    UnknownEventTypeError,
    UnsupportedEventVersionError,
    classify_failure,
)
from nexus_ai.events.subjects import Subject, build_subject, parse_subject

__all__ = [
    "EventContractError",
    "EventEnvelope",
    "EventPlatformError",
    "EventPublishError",
    "EventScope",
    "EventTenantScopeError",
    "FailureClass",
    "HandlerRetryableError",
    "HandlerTerminalError",
    "Subject",
    "SubjectValidationError",
    "UnknownEventTypeError",
    "UnsupportedEventVersionError",
    "build_subject",
    "classify_failure",
    "parse_subject",
]
