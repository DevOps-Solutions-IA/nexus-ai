"""Lightweight Core HTTP performance/concurrency smoke (MASTER PROMPT 002 section 57).

Not a capacity certification. Verifies zero unexpected responses, no context leakage and
a sane baseline latency under modest concurrency on whatever runner executes it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.anyio

_REQUESTS = 400
_CONCURRENCY = 40


async def test_core_http_smoke(app_client, capsys: pytest.CaptureFixture[str]) -> None:
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    statuses: list[int] = []
    request_ids: set[str] = set()

    async def one(index: int) -> None:
        async with semaphore:
            response = await app_client.get(
                "/health/live", headers={"X-Request-ID": f"smoke-{index:05d}-req"}
            )
            statuses.append(response.status_code)
            request_ids.add(response.headers["x-request-id"])
            assert response.headers["x-request-id"] == f"smoke-{index:05d}-req"

    started = time.perf_counter()
    await asyncio.gather(*(one(index) for index in range(_REQUESTS)))
    elapsed = time.perf_counter() - started

    assert statuses == [200] * _REQUESTS
    assert len(request_ids) == _REQUESTS  # no id bled between tasks
    mean_ms = elapsed / _REQUESTS * 1000
    # Generous ceiling: this is a smoke test, not a latency SLA tied to one runner.
    assert mean_ms < 50, f"mean {mean_ms:.2f}ms/request under {_CONCURRENCY}-way concurrency"
    capsys.readouterr()
    print(f"perf-smoke: {_REQUESTS} requests, {_CONCURRENCY}-way, mean {mean_ms:.2f} ms/req")
