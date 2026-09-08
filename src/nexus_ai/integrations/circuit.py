"""Per-integration / per-operation circuit breaker (NXS-INT-001, ADR-0060).

A breaker is keyed by ``(organization_id, integration_id, operation_key)`` and is
tenant-scoped by construction — one Organization's failing integration never trips
another's. States are the standard three:

* ``CLOSED`` — calls flow; consecutive failures are counted;
* ``OPEN`` — calls are refused with ``NXS_INT_CIRCUIT_OPEN`` until the reset window
  elapses;
* ``HALF_OPEN`` — a bounded number of trial calls are admitted; one success closes the
  breaker, one failure re-opens it.

The scope is deliberately bounded to this process (cross-node breaker coordination is
NXS-P25). State is guarded by an :class:`asyncio.Lock` per key, and the key table is
capped so a flood of distinct integrations cannot grow memory without bound.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import UUID

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.integrations.entities import CircuitState
from nexus_ai.integrations.errors import IntegrationCircuitOpenError

CircuitKey = tuple[UUID, UUID, str]
_MAX_KEYS = 50_000


@dataclass(slots=True)
class _Breaker:
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    half_open_calls: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CircuitBreakerRegistry:
    def __init__(self, settings: IntegrationsSettings, *, now: object | None = None) -> None:
        self._threshold = settings.circuit_failure_threshold
        self._reset_seconds = settings.circuit_reset_seconds
        self._half_open_max = settings.circuit_half_open_max_calls
        self._now = now or time.monotonic
        self._breakers: dict[CircuitKey, _Breaker] = {}
        self._table_lock = asyncio.Lock()

    async def _get(self, key: CircuitKey) -> _Breaker:
        breaker = self._breakers.get(key)
        if breaker is not None:
            return breaker
        async with self._table_lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                if len(self._breakers) >= _MAX_KEYS:
                    # Evict the calmest breaker to stay bounded.
                    calm = next(
                        (k for k, b in self._breakers.items() if b.state is CircuitState.CLOSED),
                        next(iter(self._breakers)),
                    )
                    del self._breakers[calm]
                breaker = _Breaker()
                self._breakers[key] = breaker
            return breaker

    async def before_call(self, key: CircuitKey) -> None:
        """Raise :class:`IntegrationCircuitOpenError` if the breaker forbids the call."""
        breaker = await self._get(key)
        async with breaker.lock:
            now = self._now()  # type: ignore[operator]
            if breaker.state is CircuitState.OPEN:
                if now - breaker.opened_at < self._reset_seconds:
                    raise IntegrationCircuitOpenError(
                        "the integration circuit is open after repeated failures"
                    )
                breaker.state = CircuitState.HALF_OPEN
                breaker.half_open_calls = 0
            if breaker.state is CircuitState.HALF_OPEN:
                if breaker.half_open_calls >= self._half_open_max:
                    raise IntegrationCircuitOpenError(
                        "the integration circuit is verifying recovery; retry shortly"
                    )
                breaker.half_open_calls += 1

    async def record_success(self, key: CircuitKey) -> None:
        breaker = await self._get(key)
        async with breaker.lock:
            breaker.failures = 0
            breaker.state = CircuitState.CLOSED
            breaker.half_open_calls = 0

    async def record_failure(self, key: CircuitKey) -> None:
        breaker = await self._get(key)
        async with breaker.lock:
            if breaker.state is CircuitState.HALF_OPEN:
                breaker.state = CircuitState.OPEN
                breaker.opened_at = self._now()  # type: ignore[operator]
                return
            breaker.failures += 1
            if breaker.failures >= self._threshold:
                breaker.state = CircuitState.OPEN
                breaker.opened_at = self._now()  # type: ignore[operator]

    async def state_of(self, key: CircuitKey) -> CircuitState:
        breaker = await self._get(key)
        return breaker.state
