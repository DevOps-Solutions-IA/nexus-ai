"""Bounded backoff math and failure classification (NXS-EVENT-007)."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import ConfigurationError
from nexus_ai.events.backoff import backoff_delay, should_dead_letter
from nexus_ai.events.errors import (
    EventContractError,
    EventPublishError,
    FailureClass,
    HandlerRetryableError,
    HandlerTerminalError,
    classify_failure,
    safe_error_code,
)


@pytest.mark.parametrize(
    "attempt,expected",
    [(0, 0.0), (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (10, 60.0), (50, 60.0)],
)
def test_backoff_is_bounded_exponential(attempt: int, expected: float) -> None:
    assert backoff_delay(attempt=attempt, base_seconds=1.0, max_seconds=60.0) == expected


def test_should_dead_letter_threshold() -> None:
    assert not should_dead_letter(attempt=4, max_attempts=8)
    assert should_dead_letter(attempt=8, max_attempts=8)
    assert should_dead_letter(attempt=12, max_attempts=8)


def test_classify_explicit_event_errors() -> None:
    assert classify_failure(HandlerRetryableError("x")) is FailureClass.RETRYABLE
    assert classify_failure(HandlerTerminalError("x")) is FailureClass.TERMINAL
    assert classify_failure(EventContractError("x")) is FailureClass.TERMINAL
    assert classify_failure(EventPublishError("x")) is FailureClass.RETRYABLE


def test_classify_transient_infrastructure_errors() -> None:
    assert classify_failure(TimeoutError()) is FailureClass.RETRYABLE
    assert classify_failure(ConnectionResetError()) is FailureClass.RETRYABLE
    assert classify_failure(OSError("broken")) is FailureClass.RETRYABLE


def test_classify_unknown_errors_are_terminal() -> None:
    assert classify_failure(ValueError("bug")) is FailureClass.TERMINAL
    assert classify_failure(KeyError("bug")) is FailureClass.TERMINAL


def test_safe_error_code_never_leaks_a_message() -> None:
    assert safe_error_code(EventPublishError("secret token abc123")) == "NXS_EVENT_PUBLISH_FAILED"
    assert safe_error_code(ConfigurationError("dsn=postgres://u:pw@h")) == (
        "NXS_CORE_CONFIGURATION_ERROR"
    )
    assert safe_error_code(RuntimeError("hunter2")) == "RuntimeError"
