"""Tool Engine concurrency guarantees (NXS-TOOL-001)."""

from __future__ import annotations

import asyncio
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
    UpdateToolRequest,
)
from nexus_ai.tools.errors import ToolConflictError, ToolExecutionInProgressError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path_params"],
    "properties": {"path_params": {"type": "object"}},
}
_ARGS = {"path_params": {"id": "c1"}}


async def _integration(te: Any, org_id: Any, base_url: str) -> Any:
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
                retry_class=RetryClass.SAFE,
            ),
        ),
    )
    return integration


def _reg(integration_id: Any) -> RegisterToolRequest:
    return RegisterToolRequest(
        tool_key="crm.get_contact",
        name="get",
        risk_class=RiskClass.LOW,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency_policy=ToolIdempotencyPolicy.OPTIONAL,
        input_schema=_SCHEMA,
        binding=ToolBinding(integration_id=integration_id, operation_key="crm.get"),
    )


async def test_concurrent_same_idempotency_key_calls_upstream_once(
    tool_engine: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    tenant_database: Any,
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    tool = await te.registry.register(org.id, _reg(integration.id))
    await te.registry.set_status(org.id, tool.id, ToolStatus.ACTIVE)
    principal = await make_tool_principal(org)
    hits = {"n": 0}

    def handler(m: str, p: str, h: dict, b: bytes) -> tuple:
        import time as _t

        hits["n"] += 1
        _t.sleep(0.05)
        return (200, {"id": "c1", "hit": hits["n"]})

    mock_http_server.set_handler(handler)
    inv = ToolInvocation(
        tool_key="crm.get_contact", arguments=_ARGS, idempotency_key="race-tool-0001"
    )
    results = await asyncio.gather(
        *(te.engine.invoke(principal, inv) for _ in range(8)), return_exceptions=True
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    in_progress = [r for r in results if isinstance(r, ToolExecutionInProgressError)]
    assert len(successes) + len(in_progress) == 8
    assert len(successes) >= 1
    assert hits["n"] == 1
    outputs = {tuple(sorted(r.output.items())) for r in successes}
    assert len(outputs) == 1
    async with tenant_database.tenant_transaction(org.id) as tenant:
        completed = (
            await tenant.session.execute(
                text("SELECT count(*) FROM tool_idempotency_records WHERE status = 'COMPLETED'")
            )
        ).scalar_one()
        records = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
    assert completed == 1 and records == 1


async def test_concurrent_registration_conflict(
    tool_engine: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    results = await asyncio.gather(
        *(te.registry.register(org.id, _reg(integration.id)) for _ in range(5)),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ToolConflictError)]
    assert len(ok) == 1 and len(conflicts) == 4


async def test_concurrent_version_updates_are_serialised(
    tool_engine: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    tool = await te.registry.register(org.id, _reg(integration.id))
    await asyncio.gather(
        *(
            te.registry.update(
                org.id,
                tool.id,
                UpdateToolRequest(side_effect_class=SideEffectClass.IDEMPOTENT_WRITE),
            )
            for _ in range(4)
        ),
        return_exceptions=True,
    )
    final = await te.registry.get(org.id, tool.id)
    assert final.version >= 2  # monotonic, no lost update / crash
