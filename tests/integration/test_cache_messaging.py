"""Real Valkey and NATS/JetStream connectivity (NXS-CACHE-001, NXS-EVENT-001)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nexus_ai.core.config import Settings
from nexus_ai.core.health import HealthStatus
from nexus_ai.infrastructure.cache import Cache
from nexus_ai.infrastructure.messaging import Messaging

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_valkey_round_trip_and_probe(integration_env: Callable[..., Settings]) -> None:
    settings = integration_env()
    cache = Cache(settings.cache)
    await cache.connect()
    try:
        await cache.client.set("nxs:it:key", "value", ex=5)
        assert await cache.client.get("nxs:it:key") == "value"
        health = await cache.probe(timeout=5)
        assert health.status is HealthStatus.UP
        assert health.name == "valkey"
    finally:
        await cache.disconnect()
        await cache.disconnect()  # idempotent


async def test_valkey_url_is_redacted(integration_env: Callable[..., Settings]) -> None:
    settings = integration_env(NXS_CACHE__URL="redis://:supersecret@127.0.0.1:16379/0")
    assert settings.cache.safe_url is not None
    assert "supersecret" not in settings.cache.safe_url


async def test_nats_connects_with_jetstream(integration_env: Callable[..., Settings]) -> None:
    settings = integration_env()
    messaging = Messaging(settings.messaging)
    await messaging.connect()
    try:
        assert messaging.is_connected
        assert messaging.jetstream_enabled is True
        health = await messaging.probe(timeout=3)
        assert health.status is HealthStatus.UP
        assert health.name == "nats"
    finally:
        await messaging.disconnect()
        await messaging.disconnect()  # idempotent
        assert not messaging.is_connected


async def test_readiness_endpoint_reports_all_dependencies_up(integration_client) -> None:
    response = await integration_client.get("/health/ready")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["dependencies"]}
    assert {"postgresql", "valkey", "nats"} <= names
    assert all(item["status"] == "UP" for item in response.json()["dependencies"])
