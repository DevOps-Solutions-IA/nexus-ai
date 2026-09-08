"""Integration Hub registry + governed execution against a controlled local server
(NXS-INT-001)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.integrations.adapters import GenericRestAdapter
from nexus_ai.integrations.credentials import CredentialType
from nexus_ai.integrations.entities import (
    AuthProfile,
    AuthProfileType,
    CreateIntegrationRequest,
    ExecutionRequest,
    GraphQLOperationSpec,
    HttpMethod,
    IntegrationStatus,
    IntegrationType,
    ParamSpec,
    ResultClass,
    RetryClass,
    SetOperationRequest,
    WebhookSignatureScheme,
)
from nexus_ai.integrations.errors import (
    IntegrationConfigInvalidError,
    IntegrationDestinationBlockedError,
    IntegrationDisabledError,
    IntegrationIdempotencyConflictError,
    IntegrationNotFoundError,
    IntegrationOperationNotFoundError,
    IntegrationUpstreamClientError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _active_integration(hub: Any, org_id: Any, base_url: str, **kw: Any) -> Any:
    integration = await hub.registry.create(
        org_id,
        CreateIntegrationRequest(
            slug=kw.get("slug", "crm"),
            name="Demo CRM",
            integration_type=IntegrationType.REST,
            base_url=base_url,
            auth_profile=kw.get("auth_profile", AuthProfile()),
        ),
    )
    return await hub.registry.set_status(org_id, integration.id, IntegrationStatus.ACTIVE)


def _get_op(
    path: str = "/contacts/{id}", retry: RetryClass = RetryClass.SAFE
) -> SetOperationRequest:
    from nexus_ai.integrations.entities import RestOperationSpec

    return SetOperationRequest(
        operation_key="crm.get_contact",
        spec=RestOperationSpec(
            method=HttpMethod.GET,
            path=path,
            path_params={"id": ParamSpec(required=True)},
            response_schema={"type": "object", "required": ["id"]},
            retry_class=retry,
        ),
    )


class TestRegistry:
    async def test_create_get_list_update_bumps_revision(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await hub.registry.create(
            org.id,
            CreateIntegrationRequest(
                slug="crm",
                name="CRM",
                integration_type=IntegrationType.CRM,
                base_url=mock_http_server.base_url,
            ),
        )
        assert integration.config_revision == 1 and integration.status is IntegrationStatus.DRAFT
        fetched = await hub.registry.get(org.id, integration.id)
        assert fetched.slug == "crm"
        listed = await hub.registry.list_integrations(org.id, limit=10, after_id=None)
        assert [i.id for i in listed] == [integration.id]

        from nexus_ai.integrations.entities import UpdateIntegrationRequest

        renamed = await hub.registry.update(
            org.id, integration.id, UpdateIntegrationRequest(name="CRM v2")
        )
        assert renamed.name == "CRM v2" and renamed.config_revision == 1  # no bump for a rename
        rebased = await hub.registry.update(
            org.id,
            integration.id,
            UpdateIntegrationRequest(base_url=f"{mock_http_server.base_url}/api"),
        )
        assert rebased.config_revision == 2  # base URL change bumps

    async def test_create_rejects_ssrf_base_url(
        self, integration_hub: Any, make_organization: Any
    ) -> None:
        org = await make_organization()
        with pytest.raises(IntegrationDestinationBlockedError):
            await integration_hub.registry.create(
                org.id,
                CreateIntegrationRequest(
                    slug="evil",
                    name="Evil",
                    integration_type=IntegrationType.REST,
                    base_url="http://169.254.169.254/latest",
                ),
            )

    async def test_slug_conflict(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        await _active_integration(integration_hub, org.id, mock_http_server.base_url)
        from nexus_ai.integrations.errors import IntegrationConflictError

        with pytest.raises(IntegrationConflictError):
            await _active_integration(integration_hub, org.id, mock_http_server.base_url)

    async def test_operations_crud_and_events(
        self,
        integration_hub: Any,
        make_organization: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        op = await hub.registry.set_operation(org.id, integration.id, _get_op())
        assert op.operation_key == "crm.get_contact"
        ops = await hub.registry.list_operations(org.id, integration.id)
        assert len(ops) == 1
        await hub.registry.remove_operation(org.id, integration.id, "crm.get_contact")
        with pytest.raises(IntegrationOperationNotFoundError):
            await hub.registry.remove_operation(org.id, integration.id, "crm.get_contact")

        async with tenant_database.tenant_transaction(org.id) as tenant:
            rows = (
                await tenant.session.execute(
                    text(
                        "SELECT event_type FROM event_outbox "
                        "WHERE event_type LIKE 'integrations.%' ORDER BY event_type"
                    )
                )
            ).all()
        types = {r[0] for r in rows}
        assert "integrations.created" in types
        assert "integrations.operation.created" in types

    async def test_graphql_operation_rejects_introspection(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url, slug="gql")
        from nexus_ai.integrations.errors import IntegrationGraphQLInvalidError

        with pytest.raises(IntegrationGraphQLInvalidError):
            await hub.registry.set_operation(
                org.id,
                integration.id,
                SetOperationRequest(
                    operation_key="gql.introspect",
                    spec=GraphQLOperationSpec(document="query { __schema { types { name } } }"),
                ),
            )


class TestExecution:
    async def test_happy_path_rest_execute(
        self,
        integration_hub: Any,
        make_organization: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1", "name": "Ada"}))
        result = await hub.service.execute(
            org.id,
            ExecutionRequest(
                integration_id=integration.id,
                operation_key="crm.get_contact",
                input={"path_params": {"id": "c1"}},
            ),
        )
        assert result.ok and result.result_class is ResultClass.SUCCESS
        assert result.output == {"id": "c1", "name": "Ada"}
        assert result.config_revision == integration.config_revision
        assert mock_http_server.requests[-1]["path"] == "/contacts/c1"

        async with tenant_database.tenant_transaction(org.id) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM integration_execution_records WHERE ok")
                )
            ).scalar_one()
        assert count == 1

    async def test_disabled_integration_refuses_execution(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        await hub.registry.set_status(org.id, integration.id, IntegrationStatus.DISABLED)
        with pytest.raises(IntegrationDisabledError):
            await hub.service.execute(
                org.id,
                ExecutionRequest(
                    integration_id=integration.id,
                    operation_key="crm.get_contact",
                    input={"path_params": {"id": "1"}},
                ),
            )

    async def test_unknown_operation(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        with pytest.raises(IntegrationOperationNotFoundError):
            await hub.service.execute(
                org.id,
                ExecutionRequest(integration_id=integration.id, operation_key="nope", input={}),
            )

    async def test_unknown_integration(self, integration_hub: Any, make_organization: Any) -> None:
        import uuid

        org = await make_organization()
        with pytest.raises(IntegrationNotFoundError):
            await integration_hub.service.execute(
                org.id,
                ExecutionRequest(integration_id=uuid.uuid4(), operation_key="op.x", input={}),
            )

    async def test_4xx_is_terminal_and_recorded(
        self,
        integration_hub: Any,
        make_organization: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        mock_http_server.set_handler(lambda m, p, h, b: (404, {"error": "missing"}))
        with pytest.raises(IntegrationUpstreamClientError):
            await hub.service.execute(
                org.id,
                ExecutionRequest(
                    integration_id=integration.id,
                    operation_key="crm.get_contact",
                    input={"path_params": {"id": "x"}},
                ),
            )
        async with tenant_database.tenant_transaction(org.id) as tenant:
            row = (
                await tenant.session.execute(
                    text(
                        "SELECT result_class, error_code FROM integration_execution_records "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).one()
            events = (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM event_outbox "
                        "WHERE event_type = 'integrations.execution.failed'"
                    )
                )
            ).scalar_one()
        assert row == ("UPSTREAM_CLIENT_ERROR", "NXS_INT_UPSTREAM_CLIENT_ERROR")
        assert events == 1

    async def test_5xx_is_retried_then_succeeds(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op(retry=RetryClass.SAFE))
        state = {"n": 0}

        def handler(m: str, p: str, h: dict, b: bytes) -> tuple:
            state["n"] += 1
            if state["n"] == 1:
                return (503, {"error": "try again"})
            return (200, {"id": "c9"})

        mock_http_server.set_handler(handler)
        result = await hub.service.execute(
            org.id,
            ExecutionRequest(
                integration_id=integration.id,
                operation_key="crm.get_contact",
                input={"path_params": {"id": "c9"}},
            ),
        )
        assert result.ok and result.retry_count == 1 and state["n"] == 2

    async def test_idempotency_replay_and_conflict(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        hits = {"n": 0}

        def handler(m: str, p: str, h: dict, b: bytes) -> tuple:
            hits["n"] += 1
            return (200, {"id": "c1", "call": hits["n"]})

        mock_http_server.set_handler(handler)
        req = ExecutionRequest(
            integration_id=integration.id,
            operation_key="crm.get_contact",
            input={"path_params": {"id": "c1"}},
            idempotency_key="idem-abc-123",
        )
        first = await hub.service.execute(org.id, req)
        second = await hub.service.execute(org.id, req)
        assert first.output == second.output and second.replayed
        assert hits["n"] == 1  # not re-sent

        with pytest.raises(IntegrationIdempotencyConflictError):
            await hub.service.execute(
                org.id,
                req.model_copy(update={"input": {"path_params": {"id": "different"}}}),
            )

    async def test_output_schema_violation_is_response_invalid(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"unexpected": True}))
        from nexus_ai.integrations.errors import IntegrationResponseInvalidError

        with pytest.raises(IntegrationResponseInvalidError):
            await hub.service.execute(
                org.id,
                ExecutionRequest(
                    integration_id=integration.id,
                    operation_key="crm.get_contact",
                    input={"path_params": {"id": "c1"}},
                ),
            )

    async def test_api_key_auth_is_applied(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        await hub.registry.store_credential(
            org.id,
            credential_ref="crm:key",
            credential_type=CredentialType.API_KEY,
            fields={"api_key": "SECRET-KEY"},
        )
        integration = await hub.registry.create(
            org.id,
            CreateIntegrationRequest(
                slug="crm",
                name="CRM",
                integration_type=IntegrationType.REST,
                base_url=mock_http_server.base_url,
                auth_profile=AuthProfile(
                    profile_type=AuthProfileType.API_KEY,
                    credential_ref="crm:key",
                    header_name="X-Api-Key",
                ),
            ),
        )
        await hub.registry.set_status(org.id, integration.id, IntegrationStatus.ACTIVE)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
        await hub.service.execute(
            org.id,
            ExecutionRequest(
                integration_id=integration.id,
                operation_key="crm.get_contact",
                input={"path_params": {"id": "c1"}},
            ),
        )
        assert mock_http_server.requests[-1]["headers"].get("X-Api-Key") == "SECRET-KEY"

    async def test_test_mode_does_not_record_or_use_idempotency(
        self,
        integration_hub: Any,
        make_organization: Any,
        mock_http_server: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(org.id, integration.id, _get_op())
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
        result = await hub.service.execute(
            org.id,
            ExecutionRequest(
                integration_id=integration.id,
                operation_key="crm.get_contact",
                input={"path_params": {"id": "c1"}},
                idempotency_key="test-key-000",
            ),
            is_test=True,
        )
        assert result.ok
        async with tenant_database.tenant_transaction(org.id) as tenant:
            recs = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM integration_execution_records")
                )
            ).scalar_one()
            idem = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM integration_idempotency_records")
                )
            ).scalar_one()
        assert recs == 0 and idem == 0


class TestAdapter:
    async def test_generic_rest_adapter_routes_through_service(
        self, integration_hub: Any, make_organization: Any, mock_http_server: Any
    ) -> None:
        from nexus_ai.integrations.entities import RestOperationSpec

        org = await make_organization()
        hub = integration_hub
        integration = await _active_integration(hub, org.id, mock_http_server.base_url)
        await hub.registry.set_operation(
            org.id,
            integration.id,
            SetOperationRequest(
                operation_key="crm.get_contact",
                spec=RestOperationSpec(
                    method=HttpMethod.GET,
                    path="/contacts/{id}",
                    path_params={"id": ParamSpec(required=True)},
                    retry_class=RetryClass.SAFE,
                ),
            ),
        )
        mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
        adapter = GenericRestAdapter(hub.service)
        result = await adapter.get_contact(org.id, integration.id, "c1")
        assert result.ok and result.output == {"id": "c1"}


async def test_config_invalid_status_transition(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _active_integration(hub, org.id, mock_http_server.base_url)
    with pytest.raises(IntegrationConfigInvalidError):
        await hub.registry.set_status(org.id, integration.id, IntegrationStatus.ERROR)


async def test_register_webhook_requires_secret_for_hmac(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    integration = await _active_integration(hub, org.id, mock_http_server.base_url)
    with pytest.raises(IntegrationConfigInvalidError):
        await hub.registry.register_webhook(
            org.id,
            integration.id,
            slug="wh",
            event_type="crm.updated",
            signature_scheme=WebhookSignatureScheme.HMAC_SHA256,
            signature_header=None,
            timestamp_header=None,
            tolerance_seconds=300,
            credential_ref=None,
        )
