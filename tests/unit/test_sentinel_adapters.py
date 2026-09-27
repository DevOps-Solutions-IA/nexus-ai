from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid7

import pytest

from nexus_ai.core.health import DependencyHealth, HealthStatus
from nexus_ai.sentinel.adapters import (
    AllowlistedHttpHealthProbe,
    CellHealthProbe,
    JetStreamHealthProbe,
    PlacementHealthProbe,
    PostgreSQLHealthProbe,
    SipTelephonyHealthProbe,
)
from nexus_ai.sentinel.errors import SentinelDenied


@pytest.mark.anyio
async def test_subsystem_probes_call_only_fixed_authority():
    actor, cell, organization, target = (uuid7() for _ in range(4))
    service = SimpleNamespace(
        inspect_cell=AsyncMock(return_value=SimpleNamespace(state="REGISTERED")),
        inspect_placement=AsyncMock(return_value=SimpleNamespace(state="ACTIVE")),
        inspect=AsyncMock(return_value=SimpleNamespace(state="ACTIVE")),
        probe=AsyncMock(return_value=DependencyHealth("nats", HealthStatus.UP, True)),
    )
    for probe in (
        CellHealthProbe(service, actor=actor, cell_id=cell),
        PlacementHealthProbe(service, actor=actor, organization_id=organization),
        SipTelephonyHealthProbe(service, actor=actor, cell_id=cell, target_id=target),
        JetStreamHealthProbe(service),
    ):
        assert (await probe()).status == HealthStatus.UP
    service.inspect_cell.assert_awaited_once_with(actor, cell)
    service.inspect_placement.assert_awaited_once_with(organization, actor)
    service.inspect.assert_awaited_once_with(actor, cell, target)


@pytest.mark.anyio
async def test_postgres_probe_uses_only_bounded_constant_query():
    from contextlib import asynccontextmanager

    session = SimpleNamespace(scalar=AsyncMock(return_value=1))

    @asynccontextmanager
    async def transaction():
        yield session

    result = await PostgreSQLHealthProbe(SimpleNamespace(transaction=transaction))()
    assert result.status == HealthStatus.UP
    assert str(session.scalar.call_args.args[0]) == "SELECT 1"


@pytest.mark.anyio
async def test_health_http_fixed_url_and_redirect_policy():
    url = "https://health.example.com/ready"
    executor = SimpleNamespace(
        _settings=SimpleNamespace(max_redirects=0),
        send=AsyncMock(return_value=SimpleNamespace(final_url=url, status_code=200)),
    )
    probe = AllowlistedHttpHealthProbe(executor, endpoint_key="health", endpoints={"health": url})
    assert (await probe()).status == HealthStatus.UP
    assert executor.send.call_args.args[0].url == url
    executor.send.return_value.final_url = "https://other.example.com/"
    with pytest.raises(SentinelDenied, match="redirect"):
        await probe()
    executor._settings.max_redirects = 1
    with pytest.raises(SentinelDenied, match="disabled"):
        AllowlistedHttpHealthProbe(executor, endpoint_key="health", endpoints={"health": url})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@example.com",
        "https://example.com/#secret",
        "https://example.com/?token=secret",
    ],
)
def test_health_endpoint_injection_denied(url):
    with pytest.raises(SentinelDenied):
        AllowlistedHttpHealthProbe(
            SimpleNamespace(_settings=SimpleNamespace(max_redirects=0)),
            endpoint_key="health",
            endpoints={"health": url},
        )
