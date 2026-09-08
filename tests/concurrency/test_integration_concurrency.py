"""Integration Hub concurrency guarantees (NXS-INT-001)."""

from __future__ import annotations

import asyncio
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
    IntegrationExecutionInProgressError,
    IntegrationIdempotencyConflictError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _active(hub: Any, org_id: Any, base_url: str, slug: str = "crm") -> Any:
    integration = await hub.registry.create(
        org_id,
        CreateIntegrationRequest(
            slug=slug,
            name=slug,
            integration_type=IntegrationType.REST,
            base_url=base_url,
        ),
    )
    return await hub.registry.set_status(org_id, integration.id, IntegrationStatus.ACTIVE)


_OP = SetOperationRequest(
    operation_key="crm.get",
    spec=RestOperationSpec(
        method=HttpMethod.GET,
        path="/c/{id}",
        path_params={"id": ParamSpec(required=True)},
        retry_class=RetryClass.SAFE,
    ),
)


async def test_concurrent_idempotent_executes_call_upstream_once(
    integration_hub: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _active(hub, org.id, mock_http_server.base_url)
    await hub.registry.set_operation(org.id, integration.id, _OP)

    hits = {"n": 0}

    def handler(m: str, p: str, h: dict, b: bytes) -> tuple:
        hits["n"] += 1
        time_sleep = 0.05
        import time as _t

        _t.sleep(time_sleep)
        return (200, {"id": "c1", "hit": hits["n"]})

    mock_http_server.set_handler(handler)
    req = ExecutionRequest(
        integration_id=integration.id,
        operation_key="crm.get",
        input={"path_params": {"id": "c1"}},
        idempotency_key="race-key-0001",
    )
    results = await asyncio.gather(
        *(hub.service.execute(org.id, req) for _ in range(8)), return_exceptions=True
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    in_progress = [r for r in results if isinstance(r, IntegrationExecutionInProgressError)]
    assert len(successes) + len(in_progress) == 8
    assert len(successes) >= 1
    assert all(isinstance(r, Exception) is False for r in successes)
    # Exactly one real upstream call, one COMPLETED idempotency row, one execution record.
    assert hits["n"] == 1
    async with tenant_database.tenant_transaction(org.id) as tenant:
        completed = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM integration_idempotency_records "
                    "WHERE status = 'COMPLETED'"
                )
            )
        ).scalar_one()
        records = (
            await tenant.session.execute(text("SELECT count(*) FROM integration_execution_records"))
        ).scalar_one()
    assert completed == 1 and records == 1
    # Every success converged on the same output.
    outputs = {tuple(sorted(r.output.items())) for r in successes}
    assert len(outputs) == 1


async def test_concurrent_operation_registration_converges(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _active(hub, org.id, mock_http_server.base_url)
    await asyncio.gather(
        *(hub.registry.set_operation(org.id, integration.id, _OP) for _ in range(5)),
        return_exceptions=True,
    )
    ops = await hub.registry.list_operations(org.id, integration.id)
    assert len(ops) == 1


async def test_concurrent_conflicting_idempotency_keys(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _active(hub, org.id, mock_http_server.base_url)
    await hub.registry.set_operation(org.id, integration.id, _OP)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))

    base = ExecutionRequest(
        integration_id=integration.id,
        operation_key="crm.get",
        input={"path_params": {"id": "c1"}},
        idempotency_key="conflict-key-1",
    )
    other = base.model_copy(update={"input": {"path_params": {"id": "c2"}}})
    results = await asyncio.gather(
        hub.service.execute(org.id, base),
        hub.service.execute(org.id, other),
        return_exceptions=True,
    )
    conflicts = [
        r
        for r in results
        if isinstance(r, IntegrationIdempotencyConflictError | IntegrationExecutionInProgressError)
    ]
    assert len(conflicts) >= 1  # the two requests cannot both win the same key


async def test_concurrent_webhook_dedup(
    integration_hub: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    from nexus_ai.integrations.entities import WebhookSignatureScheme

    org = await make_organization()
    hub = integration_hub
    integration = await _active(hub, org.id, mock_http_server.base_url, slug="wh")
    endpoint = await hub.registry.register_webhook(
        org.id,
        integration.id,
        slug="ord",
        event_type="ord.x",
        signature_scheme=WebhookSignatureScheme.NONE,
        signature_header=None,
        timestamp_header=None,
        tolerance_seconds=300,
        credential_ref=None,
    )
    body = b'{"x": 1}'
    headers = {"X-Nxs-Webhook-Id": "same-id"}
    results = await asyncio.gather(
        *(hub.webhooks.receive(endpoint.public_token, headers, body) for _ in range(6)),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, Exception)]
    assert len(accepted) == 6
    assert sum(1 for r in accepted if not r.replayed) == 1
    async with tenant_database.tenant_transaction(org.id) as tenant:
        receipts = (
            await tenant.session.execute(text("SELECT count(*) FROM webhook_receipts"))
        ).scalar_one()
        events = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE event_type = 'integrations.webhook.received'"
                )
            )
        ).scalar_one()
    assert receipts == 1 and events == 1
