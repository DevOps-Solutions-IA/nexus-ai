"""Event platform startup, health and fail-closed rules (NXS-EVENT-004, section 22)."""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import HealthStatus

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_probe_is_up_when_topology_is_ready(event_platform: Any) -> None:
    health = await event_platform.probe(timeout=3)
    assert health.status is HealthStatus.UP
    assert health.name == "event_platform"


async def test_probe_is_down_when_jetstream_is_unavailable(
    event_platform: Any, nats_messaging: Any
) -> None:
    await nats_messaging.disconnect()
    try:
        health = await event_platform.probe(timeout=3)
        assert health.status is HealthStatus.DOWN
        assert health.last_error_code == "jetstream_unavailable"
    finally:
        await nats_messaging.connect()


async def test_start_fails_closed_when_durable_transport_is_required_but_absent(
    tenant_database: Any, integration_env: Any
) -> None:
    from nexus_ai.events.service import EventPlatform
    from nexus_ai.infrastructure.messaging import Messaging

    settings = integration_env(
        NXS_ENVIRONMENT="staging",
        NXS_MESSAGING__URL="nats://127.0.0.1:5999",
        NXS_MESSAGING__CONNECT_TIMEOUT_SECONDS="1",
        NXS_TENANCY__HEADER_RESOLVER_ENABLED="false",
        NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY="false",
        NXS_AUTH__SIGNING_KEY="0" * 64,
        NXS_AUTH__ISSUER="nexus-ai",
        NXS_AUTH__AUDIENCE="nexus-ai-backend",
        NXS_AUTH__RATE_LIMIT_BACKEND="auto",
        NXS_HTTP__ALLOWED_HOSTS='["staging.nexus-ai.dev"]',
        NXS_TELEMETRY__MODE="local",
    )
    import contextlib

    messaging = Messaging(settings.messaging)
    with contextlib.suppress(Exception):
        await messaging.connect()
    platform = EventPlatform(settings, tenant_database, messaging)
    with pytest.raises(ConfigurationError, match="durable JetStream transport is required"):
        await platform.start()
    await platform.stop()
    await messaging.disconnect()


async def test_application_startup_wires_the_event_platform(integration_client: Any) -> None:
    resources = integration_client.nexus_app.state.lifespan.resources
    assert resources.event_platform is not None
    response = await integration_client.get("/health/ready")
    names = {item["name"] for item in response.json()["dependencies"]}
    assert "event_platform" in names


async def test_emit_probe_round_trips_through_jetstream(event_platform: Any) -> None:
    receipt = await event_platform.emit_probe(note="lifecycle")
    assert receipt.stream == "NXS_EVENTS"


async def test_ensure_topology_is_idempotent(event_platform: Any) -> None:
    # A second call reconciles the existing streams via update_stream rather than failing.
    await event_platform.transport.ensure_topology()
    await event_platform.transport.ensure_topology()
    assert await event_platform.transport.topology_ok() is True


async def test_graceful_stop_is_safe_to_call_twice(event_platform: Any) -> None:
    await event_platform.stop()
    await event_platform.stop()
