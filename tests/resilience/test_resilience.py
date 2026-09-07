"""Dependency failure and recovery behaviour (MASTER PROMPT 002 sections 27, 49)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai.core.health import DependencyHealth, HealthStatus
from nexus_ai.core.lifecycle import ApplicationLifespan

pytestmark = pytest.mark.anyio

_UNREACHABLE_DB = "postgresql+asyncpg://nexus:pw@127.0.0.1:5999/none"
_UNREACHABLE_CACHE = "redis://127.0.0.1:6399/0"
_UNREACHABLE_NATS = "nats://127.0.0.1:4299"


async def test_invalid_production_configuration_refuses_startup(build_settings) -> None:
    with pytest.raises(ValidationError):
        build_settings(NXS_ENVIRONMENT="production")


async def test_liveness_up_readiness_503_when_required_db_unavailable(make_client) -> None:
    async with make_client(
        NXS_DATABASE__REQUIRED="true",
        NXS_DATABASE__DSN=_UNREACHABLE_DB,
        NXS_HEALTH__PROBE_TIMEOUT_SECONDS="2",
        NXS_HEALTH__CACHE_TTL_SECONDS="0",
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        names = {item["name"]: item for item in ready.json()["dependencies"]}
        assert names["postgresql"]["status"] == "DOWN"
        assert names["postgresql"]["required"] is True


async def test_missing_required_dsn_fails_startup(build_settings) -> None:
    settings = build_settings(NXS_DATABASE__REQUIRED="true")
    lifespan = ApplicationLifespan(settings)
    with pytest.raises(Exception, match="NXS_DATABASE__DSN"):
        await lifespan.startup()
    await lifespan.shutdown()


async def test_readiness_recovers_without_restart(make_client, monkeypatch) -> None:
    async with make_client(
        NXS_CACHE__REQUIRED="true",
        NXS_CACHE__URL=_UNREACHABLE_CACHE,
        NXS_HEALTH__CACHE_TTL_SECONDS="0",
        NXS_HEALTH__PROBE_TIMEOUT_SECONDS="2",
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503

        healthy = DependencyHealth("valkey", HealthStatus.UP, True, 1.0)

        async def _healthy_probe(**_kwargs: object) -> DependencyHealth:
            return healthy

        resources = client.nexus_app.state.lifespan.resources
        monkeypatch.setattr(resources.cache, "probe", _healthy_probe)
        assert (await client.get("/health/ready")).status_code == 200


async def test_runtime_exception_yields_problem_details(make_client, add_boom_route) -> None:
    async with make_client(routes=add_boom_route) as client:
        response = await client.get("/_diagnostics/boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "NXS_CORE_INTERNAL"


async def test_optional_dependency_outage_keeps_service_ready(make_client) -> None:
    async with make_client(
        NXS_MESSAGING__REQUIRED="false",
        NXS_MESSAGING__URL=_UNREACHABLE_NATS,
        NXS_MESSAGING__CONNECT_TIMEOUT_SECONDS="1",
        NXS_HEALTH__CACHE_TTL_SECONDS="0",
    ) as client:
        ready = await client.get("/health/ready")
    assert ready.status_code == 200
