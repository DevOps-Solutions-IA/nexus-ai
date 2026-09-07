"""Async request-context isolation under concurrency (MASTER PROMPT 002 section 50)."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.anyio


async def test_request_ids_never_bleed_between_concurrent_requests(app_client) -> None:
    async def call(index: int) -> tuple[str, str]:
        response = await app_client.get(
            "/health/live", headers={"X-Request-ID": f"caller-{index:04d}-req"}
        )
        return f"caller-{index:04d}-req", response.headers["x-request-id"]

    results = await asyncio.gather(*(call(index) for index in range(200)))
    for sent, echoed in results:
        assert sent == echoed


async def test_correlation_context_does_not_leak(app_client) -> None:
    async def call(index: int) -> str:
        response = await app_client.get(
            "/health/live", headers={"X-Correlation-ID": f"corr-{index:04d}-xyz"}
        )
        return response.headers["x-correlation-id"]

    correlations = await asyncio.gather(*(call(index) for index in range(150)))
    assert sorted(correlations) == sorted({f"corr-{index:04d}-xyz" for index in range(150)})


async def test_errors_remain_correlated_under_load(make_client, add_boom_route) -> None:
    async with make_client(routes=add_boom_route) as client:

        async def call(index: int) -> tuple[str, str]:
            response = await client.get(
                "/_diagnostics/boom", headers={"X-Request-ID": f"boom-{index:04d}-id"}
            )
            return f"boom-{index:04d}-id", response.json()["request_id"]

        results = await asyncio.gather(*(call(index) for index in range(60)))
    for sent, in_body in results:
        assert sent == in_body
