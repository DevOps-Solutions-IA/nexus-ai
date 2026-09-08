"""Tool Registry + governed Tool Engine invocation (NXS-TOOL-001)."""

from __future__ import annotations

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
from nexus_ai.tools.errors import (
    ToolArgumentsInvalidError,
    ToolBindingInvalidError,
    ToolConflictError,
    ToolDisabledError,
    ToolDownstreamError,
    ToolIdempotencyConflictError,
    ToolIdempotencyRequiredError,
    ToolNotFoundError,
    ToolResultInvalidError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_GET_ARGS = {"path_params": {"id": "c1"}}
_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path_params": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        }
    },
    "required": ["path_params"],
}


async def _ready_integration(te: Any, org_id: Any, base_url: str, slug: str = "crm") -> Any:
    integration = await te.integrations.create(
        org_id,
        CreateIntegrationRequest(
            slug=slug, name=slug, integration_type=IntegrationType.REST, base_url=base_url
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


def _register(integration_id: Any, **kw: Any) -> RegisterToolRequest:
    return RegisterToolRequest(
        tool_key=kw.get("tool_key", "crm.get_contact"),
        name="Get contact",
        description="Fetch a CRM contact by id",
        risk_class=kw.get("risk_class", RiskClass.LOW),
        side_effect_class=kw.get("side_effect_class", SideEffectClass.READ_ONLY),
        idempotency_policy=kw.get("idempotency_policy", ToolIdempotencyPolicy.OPTIONAL),
        input_schema=kw.get("input_schema", _INPUT_SCHEMA),
        output_schema=kw.get("output_schema"),
        required_permissions=kw.get("required_permissions", ()),
        binding=ToolBinding(integration_id=integration_id, operation_key="crm.get"),
    )


async def _ready_tool(te: Any, org_id: Any, integration_id: Any, **kw: Any) -> Any:
    tool = await te.registry.register(org_id, _register(integration_id, **kw))
    return await te.registry.set_status(org_id, tool.id, ToolStatus.ACTIVE)


class TestRegistry:
    async def test_register_get_list_update_bumps_version(
        self, tool_engine: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        tool = await te.registry.register(org.id, _register(integration.id))
        assert tool.version == 1 and tool.status is ToolStatus.DRAFT
        assert (await te.registry.get_by_key(org.id, "crm.get_contact")).id == tool.id
        assert [t.id for t in await te.registry.list_tools(org.id, limit=10, after_id=None)] == [
            tool.id
        ]

        renamed = await te.registry.update(
            org.id, tool.id, UpdateToolRequest(name="Get contact v2")
        )
        assert renamed.name == "Get contact v2" and renamed.version == 1
        rev = await te.registry.update(
            org.id,
            tool.id,
            UpdateToolRequest(side_effect_class=SideEffectClass.IDEMPOTENT_WRITE),
        )
        assert rev.version == 2

        async with tenant_database.tenant_transaction(org.id) as tenant:
            events = {
                r[0]
                for r in (
                    await tenant.session.execute(
                        text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'tools.%'")
                    )
                ).all()
            }
        assert "tools.registered" in events and "tools.updated" in events

    async def test_register_rejects_unknown_binding(
        self, tool_engine: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        import uuid

        org = await make_organization()
        integration = await _ready_integration(tool_engine, org.id, mock_http_server.base_url)
        with pytest.raises(ToolBindingInvalidError):
            await tool_engine.registry.register(
                org.id,
                RegisterToolRequest(
                    tool_key="bad.tool",
                    name="bad",
                    input_schema=_INPUT_SCHEMA,
                    binding=ToolBinding(integration_id=integration.id, operation_key="nope.op"),
                ),
            )
        with pytest.raises(ToolBindingInvalidError):
            await tool_engine.registry.register(
                org.id,
                RegisterToolRequest(
                    tool_key="bad.tool2",
                    name="bad",
                    input_schema=_INPUT_SCHEMA,
                    binding=ToolBinding(integration_id=uuid.uuid4(), operation_key="crm.get"),
                ),
            )

    async def test_tool_key_conflict(
        self, tool_engine: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        integration = await _ready_integration(tool_engine, org.id, mock_http_server.base_url)
        await tool_engine.registry.register(org.id, _register(integration.id))
        with pytest.raises(ToolConflictError):
            await tool_engine.registry.register(org.id, _register(integration.id))

    async def test_activate_requires_risk_within_ceiling(
        self, tool_engine: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        from nexus_ai.tools.errors import ToolPolicyDeniedError

        org = await make_organization()
        integration = await _ready_integration(tool_engine, org.id, mock_http_server.base_url)
        # default ceiling is HIGH -> CRITICAL is rejected at registration
        with pytest.raises(ToolPolicyDeniedError):
            await tool_engine.registry.register(
                org.id, _register(integration.id, risk_class=RiskClass.CRITICAL)
            )

    async def test_update_surface_and_status_guards(
        self, tool_engine: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        from nexus_ai.domain.auth.rbac import PermissionKey
        from nexus_ai.tools.errors import ToolConfigInvalidError, ToolPolicyDeniedError

        org = await make_organization()
        te = tool_engine
        first = await _ready_integration(te, org.id, mock_http_server.base_url)
        second = await _ready_integration(te, org.id, mock_http_server.base_url, slug="crm2")
        tool = await te.registry.register(org.id, _register(first.id))

        # a no-op update returns the current definition unchanged
        same = await te.registry.update(org.id, tool.id, UpdateToolRequest())
        assert same.version == 1

        wide = await te.registry.update(
            org.id,
            tool.id,
            UpdateToolRequest(
                description="v2",
                risk_class=RiskClass.HIGH,
                timeout_seconds=12.0,
                output_schema={"type": "object", "additionalProperties": True},
                required_permissions=(PermissionKey.TOOL_INVOKE.value,),
                binding=ToolBinding(integration_id=second.id, operation_key="crm.get"),
            ),
        )
        assert wide.version == 2
        assert wide.risk_class is RiskClass.HIGH
        assert wide.timeout_seconds == 12.0
        assert wide.binding.integration_id == second.id

        # risk above the ceiling is refused on update too
        with pytest.raises(ToolPolicyDeniedError):
            await te.registry.update(
                org.id, tool.id, UpdateToolRequest(risk_class=RiskClass.CRITICAL)
            )

        # a side-effecting tool may not drop its idempotency key
        with pytest.raises(ToolConfigInvalidError):
            await te.registry.update(
                org.id,
                tool.id,
                UpdateToolRequest(
                    side_effect_class=SideEffectClass.EXTERNAL_EFFECT,
                    idempotency_policy=ToolIdempotencyPolicy.NONE,
                ),
            )

        # status may only be moved to ACTIVE or DISABLED
        with pytest.raises(ToolConfigInvalidError):
            await te.registry.set_status(org.id, tool.id, ToolStatus.DRAFT)

        # activation validates the (rebound) integration still exists
        activated = await te.registry.set_status(org.id, tool.id, ToolStatus.ACTIVE)
        assert activated.status is ToolStatus.ACTIVE

    async def test_update_rejects_unknown_permission_and_bad_binding(
        self, tool_engine: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        import uuid

        from nexus_ai.tools.errors import ToolConfigInvalidError

        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        tool = await te.registry.register(org.id, _register(integration.id))

        with pytest.raises(ToolConfigInvalidError):
            await te.registry.update(
                org.id, tool.id, UpdateToolRequest(required_permissions=("not:a:real:perm",))
            )
        with pytest.raises(ToolBindingInvalidError):
            await te.registry.update(
                org.id,
                tool.id,
                UpdateToolRequest(
                    binding=ToolBinding(integration_id=uuid.uuid4(), operation_key="crm.get")
                ),
            )


class TestInvocation:
    async def test_happy_path_routes_through_integration_hub(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        await _ready_tool(
            te,
            org.id,
            integration.id,
            output_schema={"type": "object", "required": ["id"]},
        )
        principal = await make_tool_principal(org)
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1", "name": "Ada"}))

        result = await te.engine.invoke(
            principal, ToolInvocation(tool_key="crm.get_contact", arguments=_GET_ARGS)
        )
        assert result.ok and result.result_class.value == "SUCCESS"
        assert result.output == {"id": "c1", "name": "Ada"}
        assert result.tool_version == 1
        assert mock_http_server.requests[-1]["path"] == "/c/c1"

        async with tenant_database.tenant_transaction(org.id) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM tool_execution_records WHERE ok")
                )
            ).scalar_one()
            events = (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM event_outbox "
                        "WHERE event_type = 'tools.invocation.completed'"
                    )
                )
            ).scalar_one()
        assert count == 1 and events == 1

    async def test_disabled_and_unregistered(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        tool = await te.registry.register(org.id, _register(integration.id))  # DRAFT
        principal = await make_tool_principal(org)
        with pytest.raises(ToolDisabledError):
            await te.engine.invoke(
                principal, ToolInvocation(tool_key="crm.get_contact", arguments=_GET_ARGS)
            )
        with pytest.raises(ToolNotFoundError):
            await te.engine.invoke(
                principal, ToolInvocation(tool_key="ghost.tool", arguments=_GET_ARGS)
            )
        assert tool.status is ToolStatus.DRAFT

    async def test_argument_schema_is_enforced(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        await _ready_tool(te, org.id, integration.id)
        principal = await make_tool_principal(org)
        with pytest.raises(ToolArgumentsInvalidError):
            await te.engine.invoke(
                principal,
                ToolInvocation(tool_key="crm.get_contact", arguments={"path_params": {}}),
            )
        with pytest.raises(ToolArgumentsInvalidError):
            await te.engine.invoke(
                principal,
                ToolInvocation(
                    tool_key="crm.get_contact",
                    arguments={"path_params": {"id": "c1"}, "smuggled": "x"},
                ),
            )

    async def test_static_arguments_cannot_be_overridden(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await te.integrations.create(
            org.id,
            CreateIntegrationRequest(
                slug="crm",
                name="crm",
                integration_type=IntegrationType.REST,
                base_url=mock_http_server.base_url,
            ),
        )
        await te.integrations.set_status(org.id, integration.id, IntegrationStatus.ACTIVE)
        await te.integrations.set_operation(
            org.id,
            integration.id,
            SetOperationRequest(
                operation_key="crm.get",
                spec=RestOperationSpec(
                    method=HttpMethod.GET,
                    path="/c/{id}",
                    path_params={"id": ParamSpec(required=True)},
                    query_params={"scope": ParamSpec()},
                    retry_class=RetryClass.SAFE,
                ),
            ),
        )
        open_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["path_params"],
            "properties": {
                "path_params": {"type": "object"},
                "query_params": {"type": "object"},
            },
        }
        tool = await te.registry.register(
            org.id, _register(integration.id, input_schema=open_schema)
        )
        # inject a fixed query param the caller cannot change
        async with tenant_database.tenant_transaction(org.id) as tenant:
            await tenant.session.execute(
                text(
                    "UPDATE tool_definitions SET static_arguments = "
                    '\'{"query_params": {"scope": "fixed"}}\'::jsonb WHERE id = :id'
                ),
                {"id": tool.id},
            )
        await te.registry.set_status(org.id, tool.id, ToolStatus.ACTIVE)
        principal = await make_tool_principal(org)
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
        await te.engine.invoke(
            principal,
            ToolInvocation(
                tool_key="crm.get_contact",
                arguments={"path_params": {"id": "c1"}, "query_params": {"scope": "attacker"}},
            ),
        )
        assert "scope=fixed" in mock_http_server.requests[-1]["path"]

    async def test_idempotency_replay_and_conflict(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        await _ready_tool(te, org.id, integration.id)
        principal = await make_tool_principal(org)
        hits = {"n": 0}

        def handler(m: str, p: str, h: dict, b: bytes) -> tuple:
            hits["n"] += 1
            return (200, {"id": "c1", "call": hits["n"]})

        mock_http_server.set_handler(handler)
        inv = ToolInvocation(
            tool_key="crm.get_contact", arguments=_GET_ARGS, idempotency_key="tool-idem-0001"
        )
        first = await te.engine.invoke(principal, inv)
        second = await te.engine.invoke(principal, inv)
        assert first.output == second.output and second.replayed and hits["n"] == 1
        with pytest.raises(ToolIdempotencyConflictError):
            await te.engine.invoke(
                principal,
                inv.model_copy(update={"arguments": {"path_params": {"id": "different"}}}),
            )

    async def test_required_idempotency_policy(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        await _ready_tool(
            te,
            org.id,
            integration.id,
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
            idempotency_policy=ToolIdempotencyPolicy.REQUIRED,
        )
        principal = await make_tool_principal(org)
        with pytest.raises(ToolIdempotencyRequiredError):
            await te.engine.invoke(
                principal, ToolInvocation(tool_key="crm.get_contact", arguments=_GET_ARGS)
            )

    async def test_downstream_error_is_wrapped(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        await _ready_tool(te, org.id, integration.id)
        principal = await make_tool_principal(org)
        mock_http_server.set_handler(lambda m, p, h, b: (404, {"error": "missing"}))
        with pytest.raises(ToolDownstreamError) as excinfo:
            await te.engine.invoke(
                principal, ToolInvocation(tool_key="crm.get_contact", arguments=_GET_ARGS)
            )
        assert excinfo.value.extensions["downstream_code"] == "NXS_INT_UPSTREAM_CLIENT_ERROR"
        async with tenant_database.tenant_transaction(org.id) as tenant:
            row = (
                await tenant.session.execute(
                    text(
                        "SELECT result_class, error_code, downstream_code FROM "
                        "tool_execution_records ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).one()
            failed = (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM event_outbox "
                        "WHERE event_type = 'tools.invocation.failed'"
                    )
                )
            ).scalar_one()
        assert row == (
            "DOWNSTREAM_ERROR",
            "NXS_TOOL_DOWNSTREAM_ERROR",
            "NXS_INT_UPSTREAM_CLIENT_ERROR",
        )
        assert failed == 1

    async def test_output_schema_violation(
        self,
        tool_engine: Any,
        make_organization: Any,
        make_tool_principal: Any,
        mock_http_server: Any,
    ) -> None:
        org = await make_organization()
        te = tool_engine
        integration = await _ready_integration(te, org.id, mock_http_server.base_url)
        await _ready_tool(
            te,
            org.id,
            integration.id,
            output_schema={
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}},
            },
        )
        principal = await make_tool_principal(org)
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"unexpected": True}))
        with pytest.raises(ToolResultInvalidError):
            await te.engine.invoke(
                principal, ToolInvocation(tool_key="crm.get_contact", arguments=_GET_ARGS)
            )
