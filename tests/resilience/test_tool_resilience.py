"""Tool Engine resilience — downstream failure mapping, no duplicate side effects
(NXS-TOOL-001)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.integrations.entities import (
    CreateIntegrationRequest,
    HttpMethod,
    IntegrationStatus,
    IntegrationType,
    ParamSpec,
    RestOperationSpec,
    RetryClass,
    SetOperationRequest,
)
from nexus_ai.tools.entities import (
    RegisterToolRequest,
    RiskClass,
    SideEffectClass,
    ToolBinding,
    ToolIdempotencyPolicy,
    ToolInvocation,
    ToolStatus,
)
from nexus_ai.tools.errors import (
    ToolDownstreamError,
    ToolRateLimitedError,
    ToolTimeoutError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path_params"],
    "properties": {"path_params": {"type": "object"}},
}
_ARGS = {"path_params": {"id": "c1"}}


async def _ready(
    te: Any,
    org_id: Any,
    base_url: str,
    *,
    op_retry: RetryClass = RetryClass.SAFE,
    op_timeout: float | None = None,
    side_effect: SideEffectClass = SideEffectClass.READ_ONLY,
) -> Any:
    integration = await te.integrations.create(
        org_id,
        CreateIntegrationRequest(
            slug="crm", name="crm", integration_type=IntegrationType.REST, base_url=base_url
        ),
    )
    await te.integrations.set_status(org_id, integration.id, IntegrationStatus.ACTIVE)
    await te.integrations.set_operation(
        org_id,
        integration.id,
        SetOperationRequest(
            operation_key="crm.get",
            spec=RestOperationSpec(
                method=HttpMethod.GET,
                path="/c/{id}",
                path_params={"id": ParamSpec(required=True)},
                retry_class=op_retry,
                timeout_seconds=op_timeout,
            ),
        ),
    )
    tool = await te.registry.register(
        org_id,
        RegisterToolRequest(
            tool_key="crm.get_contact",
            name="get",
            risk_class=RiskClass.LOW,
            side_effect_class=side_effect,
            idempotency_policy=ToolIdempotencyPolicy.OPTIONAL,
            input_schema=_SCHEMA,
            binding=ToolBinding(integration_id=integration.id, operation_key="crm.get"),
        ),
    )
    await te.registry.set_status(org_id, tool.id, ToolStatus.ACTIVE)
    return tool


def _inv() -> ToolInvocation:
    return ToolInvocation(tool_key="crm.get_contact", arguments=_ARGS)


async def test_downstream_timeout_maps_to_tool_timeout(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_timeout=1.0)
    principal = await make_tool_principal(org)

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(3)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    started = time.monotonic()
    with pytest.raises(ToolTimeoutError):
        await te.engine.invoke(principal, _inv())
    assert time.monotonic() - started < 8


async def test_downstream_rate_limit_maps_to_tool_rate_limited(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_retry=RetryClass.NON_IDEMPOTENT)
    principal = await make_tool_principal(org)
    mock_http_server.set_handler(
        lambda m, p, h, b: (429, {"error": "slow down"}, {"Retry-After": "1"})
    )
    with pytest.raises(ToolRateLimitedError):
        await te.engine.invoke(principal, _inv())


async def test_non_idempotent_downstream_5xx_is_not_retried(
    tool_engine: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    tenant_database: Any,
) -> None:
    org = await make_organization()
    te = tool_engine
    await _ready(
        te,
        org.id,
        mock_http_server.base_url,
        op_retry=RetryClass.NON_IDEMPOTENT,
        side_effect=SideEffectClass.NON_IDEMPOTENT_WRITE,
    )
    principal = await make_tool_principal(org)
    calls = {"n": 0}

    def handler(m: str, p: str, h: dict, b: bytes) -> tuple:
        calls["n"] += 1
        return (503, {"error": "down"})

    mock_http_server.set_handler(handler)
    with pytest.raises(ToolDownstreamError):
        await te.engine.invoke(principal, _inv())
    assert calls["n"] == 1  # a NON_IDEMPOTENT operation is never retried after a 5xx
    async with tenant_database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text(
                    "SELECT result_class, downstream_code FROM tool_execution_records "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).one()
    assert row == ("DOWNSTREAM_ERROR", "NXS_INT_UPSTREAM_SERVER_ERROR")


async def test_downstream_circuit_open_propagates(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_retry=RetryClass.NON_IDEMPOTENT)
    principal = await make_tool_principal(org)
    mock_http_server.set_handler(lambda m, p, h, b: (500, {"e": 1}))
    threshold = te.settings.integrations.circuit_failure_threshold
    for _ in range(threshold):
        with pytest.raises(ToolDownstreamError):
            await te.engine.invoke(principal, _inv())
    hits_before = len(mock_http_server.requests)
    with pytest.raises(ToolDownstreamError) as excinfo:
        await te.engine.invoke(principal, _inv())
    assert excinfo.value.extensions.get("downstream_code") == "NXS_INT_CIRCUIT_OPEN"
    assert len(mock_http_server.requests) == hits_before  # short-circuited, no upstream hit
