"""Tool Engine resilience — downstream failure mapping, no duplicate side effects
(NXS-TOOL-001)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.integrations.credentials import CredentialType
from nexus_ai.integrations.entities import (
    AuthProfile,
    AuthProfileType,
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
    ToolExecutionFailedError,
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
    tool_timeout: float | None = None,
    auth: AuthProfile | None = None,
) -> Any:
    integration = await te.integrations.create(
        org_id,
        CreateIntegrationRequest(
            slug="crm",
            name="crm",
            integration_type=IntegrationType.REST,
            base_url=base_url,
            auth_profile=auth or AuthProfile(),
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
            timeout_seconds=tool_timeout,
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
    """The NXS-P07 operation timeout path: the integration's own bound fires and the
    Tool Engine maps NXS_INT_TIMEOUT -> NXS_TOOL_TIMEOUT (timeout_scope 'integration')."""
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_timeout=1.0)
    principal = await make_tool_principal(org)

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(3)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    started = time.monotonic()
    with pytest.raises(ToolTimeoutError) as excinfo:
        await te.engine.invoke(principal, _inv())
    assert time.monotonic() - started < 8
    assert excinfo.value.extensions.get("timeout_scope") == "integration"


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


# --- NXS-P08 ToolDefinition.timeout_seconds enforcement (independent of NXS-P07) ------


async def test_tool_timeout_seconds_enforced_at_engine_boundary(
    tool_engine: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    tenant_database: Any,
) -> None:
    """A: the P08 tool timeout is a real upper bound. The P07 operation timeout is set
    materially longer (8 s); the tool timeout (0.5 s) fires first, deterministically maps
    to NXS_TOOL_TIMEOUT, records a failure receipt + safe event, and leaks no secret."""
    org = await make_organization()
    te = tool_engine
    await te.integrations.store_credential(
        org.id,
        credential_ref="crm:key",
        credential_type=CredentialType.API_KEY,
        fields={"api_key": "TOP-SECRET-TIMEOUT-KEY"},
    )
    await _ready(
        te,
        org.id,
        mock_http_server.base_url,
        op_timeout=8.0,
        tool_timeout=0.5,
        auth=AuthProfile(
            profile_type=AuthProfileType.API_KEY,
            credential_ref="crm:key",
            header_name="X-Api-Key",
        ),
    )
    principal = await make_tool_principal(org)

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(2.0)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    started = time.monotonic()
    with pytest.raises(ToolTimeoutError) as excinfo:
        await te.engine.invoke(principal, _inv())
    elapsed = time.monotonic() - started

    assert 0.4 <= elapsed < 4.0  # the 0.5 s tool bound fired, not the 8 s P07 bound
    assert excinfo.value.code == "NXS_TOOL_TIMEOUT"
    assert excinfo.value.extensions.get("timeout_scope") == "tool"
    assert excinfo.value.extensions.get("timeout_seconds") == 0.5
    assert len(mock_http_server.requests) == 1  # exactly one governed P07 attempt, no bypass

    async with tenant_database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text(
                    "SELECT result_class, error_code, downstream_code, duration_ms "
                    "FROM tool_execution_records ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).one()
        records = (
            await tenant.session.execute(
                text("SELECT row_to_json(t)::text FROM tool_execution_records t")
            )
        ).all()
        events = (
            await tenant.session.execute(
                text("SELECT envelope::text FROM event_outbox WHERE event_type LIKE 'tools.%'")
            )
        ).all()
    assert row[0] == "TIMEOUT" and row[1] == "NXS_TOOL_TIMEOUT" and row[2] is None
    assert row[3] >= 400  # duration reflects the ~0.5 s bound, not a zero
    assert any("tools.invocation.failed" in e[0] for e in events)
    assert all("TOP-SECRET" not in r[0] for r in records)
    assert all("TOP-SECRET" not in e[0] for e in events)


async def test_no_tool_timeout_leaves_p07_behavior_unchanged(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """B: with tool.timeout_seconds = None the engine adds no bound — a fast upstream
    succeeds and a slow upstream is bounded only by the P07 operation timeout."""
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_timeout=1.0, tool_timeout=None)
    principal = await make_tool_principal(org)

    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1", "ok": True}))
    ok = await te.engine.invoke(principal, _inv())
    assert ok.ok and ok.output == {"id": "c1", "ok": True}

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(3)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    with pytest.raises(ToolTimeoutError) as excinfo:
        await te.engine.invoke(principal, _inv())
    assert excinfo.value.extensions.get("timeout_scope") == "integration"


async def test_shorter_p07_timeout_still_fires_when_tool_timeout_is_longer(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """C: the stricter bound wins. P07 op timeout 0.5 s, tool timeout 5 s, upstream sleeps
    3 s -> the P07 bound fires and is surfaced as NXS_TOOL_TIMEOUT (scope 'integration')."""
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_timeout=0.5, tool_timeout=5.0)
    principal = await make_tool_principal(org)

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(3)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    started = time.monotonic()
    with pytest.raises(ToolTimeoutError) as excinfo:
        await te.engine.invoke(principal, _inv())
    assert time.monotonic() - started < 4.0  # the 0.5 s P07 bound, not the 5 s tool bound
    assert excinfo.value.extensions.get("timeout_scope") == "integration"


async def test_tool_timeout_finalises_idempotency_as_failed(
    tool_engine: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    tenant_database: Any,
) -> None:
    """E: an idempotent invocation that hits the P08 tool timeout finalises its claim as
    FAILED (never PENDING, never COMPLETED); the same key then follows the documented
    failed-invocation rule and does NOT re-run the external call."""
    org = await make_organization()
    te = tool_engine
    await _ready(te, org.id, mock_http_server.base_url, op_timeout=8.0, tool_timeout=0.5)
    principal = await make_tool_principal(org)

    def slow(m: str, p: str, h: dict, b: bytes) -> tuple:
        time.sleep(2.0)
        return (200, {"id": "c1"})

    mock_http_server.set_handler(slow)
    inv = ToolInvocation(
        tool_key="crm.get_contact", arguments=_ARGS, idempotency_key="tool-timeout-idem-1"
    )
    with pytest.raises(ToolTimeoutError):
        await te.engine.invoke(principal, inv)
    hits_after_first = len(mock_http_server.requests)
    assert hits_after_first == 1

    async with tenant_database.tenant_transaction(org.id) as tenant:
        status = (
            await tenant.session.execute(
                text(
                    "SELECT status FROM tool_idempotency_records "
                    "WHERE idempotency_key = 'tool-timeout-idem-1'"
                )
            )
        ).scalar_one()
    assert status == "FAILED"

    # same key, same fingerprint -> deterministic failed-invocation error, no second call
    with pytest.raises(ToolExecutionFailedError):
        await te.engine.invoke(principal, inv)
    assert len(mock_http_server.requests) == hits_after_first  # no accidental re-execution
