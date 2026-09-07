"""Dependency health model and readiness aggregation (NXS-HEALTH-001)."""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai.core.health import (
    DependencyHealth,
    HealthStatus,
    ReadinessEvaluator,
    timed_probe,
)

pytestmark = pytest.mark.anyio


def _probe(name: str, status: HealthStatus, required: bool = True):
    async def _run() -> DependencyHealth:
        return DependencyHealth(name, status, required)

    return _run


async def test_ready_when_all_required_up() -> None:
    evaluator = ReadinessEvaluator(
        [_probe("db", HealthStatus.UP), _probe("cache", HealthStatus.UP)],
        ttl_seconds=0,
        probe_timeout=1,
    )
    report = await evaluator.evaluate()
    assert report.ready
    assert report.status_text == "READY"


async def test_not_ready_when_required_down() -> None:
    evaluator = ReadinessEvaluator(
        [_probe("db", HealthStatus.DOWN), _probe("cache", HealthStatus.UP)],
        ttl_seconds=0,
        probe_timeout=1,
    )
    report = await evaluator.evaluate()
    assert not report.ready
    assert report.as_payload()["status"] == "NOT_READY"


async def test_optional_down_does_not_block_readiness() -> None:
    evaluator = ReadinessEvaluator(
        [_probe("nats", HealthStatus.DOWN, required=False)], ttl_seconds=0, probe_timeout=1
    )
    assert (await evaluator.evaluate()).ready


async def test_ttl_cache_reuses_result() -> None:
    calls = 0

    async def counting() -> DependencyHealth:
        nonlocal calls
        calls += 1
        return DependencyHealth("db", HealthStatus.UP, True)

    evaluator = ReadinessEvaluator([counting], ttl_seconds=60, probe_timeout=1)
    await evaluator.evaluate()
    await evaluator.evaluate()
    assert calls == 1
    await evaluator.evaluate(use_cache=False)
    assert calls == 2


async def test_timed_probe_times_out_safely() -> None:
    async def slow() -> None:
        await asyncio.sleep(5)

    health = await timed_probe("db", True, slow, timeout=0.05)
    assert health.status is HealthStatus.DOWN
    assert health.last_error_code == "probe_timeout"


async def test_timed_probe_classifies_exception_without_leaking() -> None:
    async def broken() -> None:
        raise RuntimeError("connection refused to 10.0.0.5:5432 password=hunter2")

    health = await timed_probe("db", True, broken, timeout=1)
    assert health.status is HealthStatus.DOWN
    assert health.last_error_code == "RuntimeError"
    assert "hunter2" not in str(health.as_payload())
