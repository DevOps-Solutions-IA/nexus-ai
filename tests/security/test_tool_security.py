"""Tool Engine security matrix (NXS-TOOL-001)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.domain.auth.rbac import RoleKey
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
    ToolArgumentsInvalidError,
    ToolDisabledError,
    ToolIdempotencyConflictError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path_params"],
    "properties": {
        "path_params": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        }
    },
}
_ARGS = {"path_params": {"id": "c1"}}


async def _integration(
    te: Any, org_id: Any, base_url: str, *, auth: AuthProfile | None = None
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
                retry_class=RetryClass.SAFE,
            ),
        ),
    )
    return integration


def _reg(integration_id: Any, **kw: Any) -> RegisterToolRequest:
    return RegisterToolRequest(
        tool_key=kw.get("tool_key", "crm.get_contact"),
        name="get",
        risk_class=kw.get("risk_class", RiskClass.LOW),
        side_effect_class=kw.get("side_effect_class", SideEffectClass.READ_ONLY),
        idempotency_policy=kw.get("idempotency_policy", ToolIdempotencyPolicy.OPTIONAL),
        input_schema=_SCHEMA,
        required_permissions=kw.get("required_permissions", ()),
        binding=ToolBinding(integration_id=integration_id, operation_key="crm.get"),
    )


async def _active_tool(te: Any, org_id: Any, integration_id: Any, **kw: Any) -> Any:
    tool = await te.registry.register(org_id, _reg(integration_id, **kw))
    return await te.registry.set_status(org_id, tool.id, ToolStatus.ACTIVE)


async def test_unregistered_and_disabled_tool_rejected(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    principal = await make_tool_principal(org)
    with pytest.raises(ToolNotFoundError):
        await te.engine.invoke(
            principal, ToolInvocation(tool_key="not.registered", arguments=_ARGS)
        )
    draft = await te.registry.register(org.id, _reg(integration.id))
    with pytest.raises(ToolDisabledError):
        await te.engine.invoke(principal, ToolInvocation(tool_key=draft.tool_key, arguments=_ARGS))


async def test_cross_tenant_lookup_and_invoke_fail_closed(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    te = tool_engine
    org_a = await make_organization()
    org_b = await make_organization()
    integration = await _integration(te, org_a.id, mock_http_server.base_url)
    tool = await _active_tool(te, org_a.id, integration.id)
    principal_b = await make_tool_principal(org_b)
    # org B cannot see org A's tool
    with pytest.raises(ToolNotFoundError):
        await te.registry.get(org_b.id, tool.id)
    # and cannot invoke it — the invocation carries no org, the principal's org is used
    with pytest.raises(ToolNotFoundError):
        await te.engine.invoke(principal_b, ToolInvocation(tool_key=tool.tool_key, arguments=_ARGS))


async def test_permission_bypass_is_refused(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    # the tool requires customer:update, which org_member does NOT hold
    await _active_tool(te, org.id, integration.id, required_permissions=("customer:update",))
    member = await make_tool_principal(org, role=RoleKey.ORG_MEMBER)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
    with pytest.raises(ToolPermissionDeniedError):
        await te.engine.invoke(member, ToolInvocation(tool_key="crm.get_contact", arguments=_ARGS))


async def test_forged_organization_id_is_impossible(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """The ToolInvocation contract has no organization field, so a caller cannot target
    another Organization — the trusted org comes from the principal only."""
    assert "organization_id" not in ToolInvocation.model_fields
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    await _active_tool(te, org.id, integration.id)
    with pytest.raises(ValueError):
        ToolInvocation(  # type: ignore[call-arg]
            tool_key="crm.get_contact", arguments=_ARGS, organization_id=str(uuid.uuid4())
        )


async def test_schema_bypass_and_extra_arguments(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    await _active_tool(te, org.id, integration.id)
    principal = await make_tool_principal(org)
    for bad in (
        {"path_params": {"id": 123}},  # wrong type
        {"path_params": {"id": "c1"}, "url": "http://169.254.169.254/"},  # URL injection
        {"path_params": {"id": "c1"}, "method": "DELETE"},  # method injection
        {"path_params": {"id": "c1"}, "headers": {"X-Evil": "1"}},  # header injection
        {"path_params": {"id": "c1"}, "query": "{__schema}"},  # graphql injection
        {"path_params": {"id": "c1", "operation_key": "crm.delete"}},  # op tampering
    ):
        with pytest.raises(ToolArgumentsInvalidError):
            await te.engine.invoke(
                principal, ToolInvocation(tool_key="crm.get_contact", arguments=bad)
            )


async def test_no_secret_or_credential_exposure(
    tool_engine: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    tenant_database: Any,
) -> None:
    org = await make_organization()
    te = tool_engine
    await te.integrations.store_credential(
        org.id,
        credential_ref="crm:key",
        credential_type=CredentialType.API_KEY,
        fields={"api_key": "TOP-SECRET-TOOL-KEY"},
    )
    integration = await _integration(
        te,
        org.id,
        mock_http_server.base_url,
        auth=AuthProfile(
            profile_type=AuthProfileType.API_KEY, credential_ref="crm:key", header_name="X-Api-Key"
        ),
    )
    await _active_tool(te, org.id, integration.id)
    principal = await make_tool_principal(org)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
    result = await te.engine.invoke(
        principal, ToolInvocation(tool_key="crm.get_contact", arguments=_ARGS)
    )
    dumped = result.model_dump_json()
    assert "TOP-SECRET" not in dumped and "X-Api-Key" not in dumped
    async with tenant_database.tenant_transaction(org.id) as tenant:
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
    assert all("TOP-SECRET" not in r[0] for r in records)
    assert events and all("TOP-SECRET" not in e[0] for e in events)
    # the mock server DID receive the key (proving the tool routed through P07 auth)
    assert mock_http_server.requests[-1]["headers"].get("X-Api-Key") == "TOP-SECRET-TOOL-KEY"


async def test_untrusted_result_poisoning_is_caught(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    from nexus_ai.tools.errors import ToolResultInvalidError

    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    await _active_tool(
        te,
        org.id,
        integration.id,
    )
    # tighten output schema after activation via update
    tool = await te.registry.get_by_key(org.id, "crm.get_contact")
    from nexus_ai.tools.entities import UpdateToolRequest

    await te.registry.update(
        org.id,
        tool.id,
        UpdateToolRequest(
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            }
        ),
    )
    principal = await make_tool_principal(org)
    mock_http_server.set_handler(
        lambda m, p, h, b: (200, {"id": "c1", "__proto__": {"admin": True}, "evil": "x"})
    )
    with pytest.raises(ToolResultInvalidError):
        await te.engine.invoke(
            principal, ToolInvocation(tool_key="crm.get_contact", arguments=_ARGS)
        )


async def test_idempotency_key_reuse_with_different_payload(
    tool_engine: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    te = tool_engine
    integration = await _integration(te, org.id, mock_http_server.base_url)
    await _active_tool(te, org.id, integration.id)
    principal = await make_tool_principal(org)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
    await te.engine.invoke(
        principal,
        ToolInvocation(
            tool_key="crm.get_contact",
            arguments={"path_params": {"id": "c1"}},
            idempotency_key="reuse-key-9999",
        ),
    )
    with pytest.raises(ToolIdempotencyConflictError):
        await te.engine.invoke(
            principal,
            ToolInvocation(
                tool_key="crm.get_contact",
                arguments={"path_params": {"id": "c2"}},
                idempotency_key="reuse-key-9999",
            ),
        )


async def test_forged_cross_tenant_tool_binding_fk_is_refused(
    tool_engine: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    from sqlalchemy.exc import DBAPIError

    te = tool_engine
    org_a = await make_organization()
    org_b = await make_organization()
    integration_a = await _integration(te, org_a.id, mock_http_server.base_url)
    with pytest.raises(DBAPIError):  # RLS WITH CHECK + composite FK
        async with tenant_database.tenant_transaction(org_b.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO tool_definitions (id, organization_id, integration_id, "
                    "operation_key, tool_key, name, version, status, risk_class, "
                    "side_effect_class, idempotency_policy, input_schema, required_permissions, "
                    "binding_type, static_arguments, created_at, updated_at) VALUES "
                    "(:id, :org, :integ, 'crm.get', 't.x', 'x', 1, 'DRAFT', 'LOW', 'READ_ONLY', "
                    "'NONE', '{}'::jsonb, '[]'::jsonb, 'INTEGRATION', '{}'::jsonb, now(), now())"
                ),
                {"id": uuid.uuid4(), "org": org_b.id, "integ": integration_a.id},
            )
