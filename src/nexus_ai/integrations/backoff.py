"""Retry classification and bounded backoff for outbound integration calls
(NXS-INT-001, ADR-0060).

Nexus never blindly retries. An operation declares a :class:`RetryClass`; only that
class, the failure kind and the elapsed budget decide whether a second attempt happens:

* ``SAFE`` — no side effects (a GET): retry on any transient failure, including a
  connection error that may have partially sent;
* ``IDEMPOTENT`` — a repeat is harmless (PUT / DELETE, or POST with a provider
  idempotency key): retry on transient failures;
* ``NON_IDEMPOTENT`` — a repeat could double an effect (a plain POST): retry ONLY when
  the request provably never reached the server (connection refused / DNS / TLS), never
  after a timeout or a 5xx.

Backoff is exponential with full jitter, capped by ``retry_max_delay_seconds`` and by a
total ``retry_max_elapsed_seconds`` budget. A ``Retry-After`` from the upstream always
wins (clamped to the max delay).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.integrations.entities import RetryClass


class FailureKind(StrEnum):
    #: The request provably never reached the server.
    PRE_SEND = "PRE_SEND"  # DNS failure, connection refused, TLS handshake failure
    #: The request may or may not have been processed.
    IN_FLIGHT = "IN_FLIGHT"  # read timeout, connection reset mid-response
    #: The server processed the request and returned a retryable status.
    UPSTREAM_5XX = "UPSTREAM_5XX"
    UPSTREAM_429 = "UPSTREAM_429"
    #: Terminal — never retry.
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    delay_seconds: float = 0.0
    reason: str = ""


class RetryPolicy:
    def __init__(self, settings: IntegrationsSettings) -> None:
        self._max_attempts = settings.retry_max_attempts
        self._base = settings.retry_base_delay_seconds
        self._max_delay = settings.retry_max_delay_seconds
        self._max_elapsed = settings.retry_max_elapsed_seconds

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _backoff(self, attempt: int) -> float:
        """Full-jitter exponential backoff for ``attempt`` (1-based)."""
        ceiling: float = min(self._max_delay, self._base * (2.0 ** (attempt - 1)))
        # secrets.randbelow for a jitter fraction — no insecure RNG (ruff S311).
        jitter: float = secrets.randbelow(1_000_000) / 1_000_000
        return float(round(ceiling * jitter, 4))

    def decide(
        self,
        *,
        retry_class: RetryClass,
        failure: FailureKind,
        attempt: int,
        elapsed_seconds: float,
        retry_after_seconds: float | None = None,
    ) -> RetryDecision:
        if attempt >= self._max_attempts:
            return RetryDecision(False, reason="max_attempts")
        if elapsed_seconds >= self._max_elapsed:
            return RetryDecision(False, reason="max_elapsed")
        if failure is FailureKind.TERMINAL:
            return RetryDecision(False, reason="terminal")

        if failure is FailureKind.PRE_SEND:
            retryable = True  # never reached the server — safe for every class
        elif retry_class is RetryClass.SAFE:
            retryable = True
        elif retry_class is RetryClass.IDEMPOTENT:
            retryable = failure in (
                FailureKind.IN_FLIGHT,
                FailureKind.UPSTREAM_5XX,
                FailureKind.UPSTREAM_429,
            )
        else:  # NON_IDEMPOTENT
            retryable = False  # already handled PRE_SEND above

        if not retryable:
            return RetryDecision(False, reason=f"not_retryable:{retry_class}:{failure}")

        if retry_after_seconds is not None:
            delay = max(0.0, min(retry_after_seconds, self._max_delay))
        else:
            delay = self._backoff(attempt)
        if elapsed_seconds + delay >= self._max_elapsed:
            return RetryDecision(False, reason="would_exceed_budget")
        return RetryDecision(True, delay_seconds=delay, reason="retry")
