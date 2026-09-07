"""Typed liveness/readiness health subsystem (NXS-HEALTH-001).

Liveness answers "is this process alive?" and must not depend on external systems.
Readiness answers "can this instance safely take traffic?" and aggregates typed probes.
Probe results carry only safe fields — never raw exceptions, DSNs or credentials.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Probe = Callable[[], Awaitable["DependencyHealth"]]


class HealthStatus(enum.StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    name: str
    status: HealthStatus
    required: bool
    latency_ms: float | None = None
    last_error_code: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status.value,
            "required": self.required,
        }
        if self.latency_ms is not None:
            payload["latency_ms"] = round(self.latency_ms, 2)
        if self.last_error_code is not None:
            payload["last_error_code"] = self.last_error_code
        return payload


@dataclass(frozen=True, slots=True)
class HealthReport:
    ready: bool
    dependencies: tuple[DependencyHealth, ...]

    @property
    def status_text(self) -> str:
        return "READY" if self.ready else "NOT_READY"

    def dependency_payloads(self) -> list[dict[str, object]]:
        return [dependency.as_payload() for dependency in self.dependencies]

    def as_payload(self) -> dict[str, object]:
        return {"status": self.status_text, "dependencies": self.dependency_payloads()}


async def timed_probe(
    name: str,
    required: bool,
    check: Callable[[], Awaitable[None]],
    *,
    timeout: float,
) -> DependencyHealth:
    """Run ``check`` with a hard timeout and translate the outcome to a safe result."""
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check(), timeout=timeout)
    except TimeoutError:
        return DependencyHealth(name, HealthStatus.DOWN, required, None, "probe_timeout")
    except Exception as exc:
        code = type(exc).__name__
        return DependencyHealth(name, HealthStatus.DOWN, required, None, code[:64])
    latency_ms = (time.perf_counter() - started) * 1000
    return DependencyHealth(name, HealthStatus.UP, required, latency_ms)


class ReadinessEvaluator:
    """Aggregates dependency probes with a short TTL cache for frequent probing."""

    def __init__(self, probes: list[Probe], *, ttl_seconds: float, probe_timeout: float) -> None:
        self._probes = probes
        self._ttl = ttl_seconds
        self._probe_timeout = probe_timeout
        self._cache: HealthReport | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def evaluate(self, *, use_cache: bool = True) -> HealthReport:
        now = time.monotonic()
        if use_cache and self._cache is not None and (now - self._cached_at) < self._ttl:
            return self._cache
        async with self._lock:
            now = time.monotonic()
            if use_cache and self._cache is not None and (now - self._cached_at) < self._ttl:
                return self._cache
            report = await self._run()
            self._cache = report
            self._cached_at = time.monotonic()
            return report

    async def _run(self) -> HealthReport:
        if not self._probes:
            return HealthReport(ready=True, dependencies=())
        results = await asyncio.gather(*(probe() for probe in self._probes))
        ready = all(item.status is HealthStatus.UP for item in results if item.required)
        return HealthReport(ready=ready, dependencies=tuple(results))
