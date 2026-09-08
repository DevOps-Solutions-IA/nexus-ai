"""Integration Hub resilience — bounded timeouts, bounded retry, circuit recovery
(NXS-INT-001)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.integrations.entities import (
    CreateIntegrationRequest,
    ExecutionRequest,
    HttpMethod,
    IntegrationStatus,
    IntegrationType,
    ParamSpec,
    RestOperationSpec,
    RetryClass,
    SetOperationRequest,
)
from nexus_ai.integrations.errors import (
    IntegrationCircuitOpenError,
    IntegrationTimeoutError,
    IntegrationUpstreamServerError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _ready(
    hub: Any, org_id: Any, base_url: str, retry: RetryClass, *, timeout: float | None = None
) -> Any:
    integration = await hub.registry.create(
        org_id,
        CreateIntegrationRequest(
            slug="crm",
            name="crm",
            integration_type=IntegrationType.REST,
            base_url=base_url,
        ),
    )
    await hub.registry.set_status(org_id, integration.id, IntegrationStatus.ACTIVE)
    await hub.registry.set_operation(
        org_id,
        integration.id,
        SetOperationRequest(
            operation_key="crm.get",
            spec=RestOperationSpec(
                method=HttpMethod.GET,
                path="/c/{id}",
                path_params={"id": ParamSpec(required=True)},
                retry_class=retry,
                timeout_seconds=timeout,
            ),
        ),
    )
    return integration


def _req(integration_id: Any) -> ExecutionRequest:
    return ExecutionRequest(
        integration_id=integration_id,
        operation_key="crm.get",
        input={"path_params": {"id": "c1"}},
    )


async def test_total_timeout_bounds_a_slow_upstream(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _ready(hub, org.id, mock_http_server.base_url, RetryClass.SAFE, timeout=1.0)

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(3)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    started = time.monotonic()
    with pytest.raises(IntegrationTimeoutError):
        await hub.service.execute(org.id, _req(integration.id))
    assert time.monotonic() - started < 6  # bounded, not hanging


async def test_retry_is_bounded_by_max_attempts(
    integration_hub: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _ready(hub, org.id, mock_http_server.base_url, RetryClass.SAFE)
    calls = {"n": 0}

    def always_500(m: str, p: str, h: dict, b: bytes) -> tuple:
        calls["n"] += 1
        return (503, {"err": "down"})

    mock_http_server.set_handler(always_500)
    with pytest.raises(IntegrationUpstreamServerError):
        await hub.service.execute(org.id, _req(integration.id))
    assert calls["n"] <= hub.settings.integrations.retry_max_attempts
    async with tenant_database.tenant_transaction(org.id) as tenant:
        rc = (
            await tenant.session.execute(
                text(
                    "SELECT retry_count FROM integration_execution_records "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
    assert rc == calls["n"] - 1


async def test_circuit_opens_and_short_circuits(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _ready(hub, org.id, mock_http_server.base_url, RetryClass.NON_IDEMPOTENT)
    mock_http_server.set_handler(lambda m, p, h, b: (500, {"e": 1}))

    threshold = hub.settings.integrations.circuit_failure_threshold
    for _ in range(threshold):
        with pytest.raises(IntegrationUpstreamServerError):
            await hub.service.execute(org.id, _req(integration.id))
    # Now the breaker is open — the next call is refused fast, without hitting upstream.
    hits_before = len(mock_http_server.requests)
    with pytest.raises(IntegrationCircuitOpenError):
        await hub.service.execute(org.id, _req(integration.id))
    assert len(mock_http_server.requests) == hits_before


async def test_large_response_is_rejected(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _ready(hub, org.id, mock_http_server.base_url, RetryClass.SAFE)
    big = (
        b'{"id": "c1", "pad": "'
        + b"z" * (hub.settings.integrations.max_response_bytes + 10)
        + b'"}'
    )
    mock_http_server.set_handler(lambda m, p, h, b: (200, big))
    from nexus_ai.integrations.errors import IntegrationResponseTooLargeError

    with pytest.raises(IntegrationResponseTooLargeError):
        await hub.service.execute(org.id, _req(integration.id))
