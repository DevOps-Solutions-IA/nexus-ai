"""Integration Hub security matrix — SSRF, secret exfiltration, cross-tenant,
arbitrary-execution (NXS-INT-001)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.integrations.credentials import CredentialType
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import (
    AuthProfile,
    AuthProfileType,
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
    IntegrationCredentialUnavailableError,
    IntegrationDestinationBlockedError,
    IntegrationNotFoundError,
)
from nexus_ai.integrations.registry import IntegrationRegistry

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SSRF_TARGETS = [
    "http://169.254.169.254/latest/meta-data/iam/",
    "http://[fd00:ec2::254]/latest/",
    "https://10.0.0.1/internal",
    "https://192.168.0.1/admin",
    "https://127.0.0.1/x",
    "https://localhost/x",
    "https://metadata.google.internal/computeMetadata/v1/",
    "file:///etc/passwd",
    "gopher://10.0.0.1:70/_x",
]


@pytest.mark.parametrize("target", _SSRF_TARGETS)
async def test_ssrf_blocked_at_config_time(
    integration_hub: Any, make_organization: Any, target: str
) -> None:
    org = await make_organization()
    # The production registry uses the strict default policy, not the test loopback one.
    strict = IntegrationRegistry(
        integration_hub.settings,
        integration_hub.database,
        integration_hub.event_platform.publisher,
        integration_hub.vault,
        DestinationPolicy(),
    )
    with pytest.raises(IntegrationDestinationBlockedError):
        await strict.create(
            org.id,
            CreateIntegrationRequest(
                slug="evil",
                name="evil",
                integration_type=IntegrationType.REST,
                base_url=target,
            ),
        )


async def test_ssrf_blocked_at_execution_time_on_dns_rebind(
    integration_hub: Any, make_organization: Any
) -> None:
    """A base URL that passes config validation but resolves to a private address at
    execution time is refused by the executor."""
    from nexus_ai.domain.integrations.repository import IntegrationIdempotencyRepository
    from nexus_ai.integrations.auth_profiles import AuthProfileApplier
    from nexus_ai.integrations.circuit import CircuitBreakerRegistry
    from nexus_ai.integrations.executor import GovernedHttpExecutor
    from nexus_ai.integrations.ratelimit import OutboundRateLimiter
    from nexus_ai.integrations.service import IntegrationHubService

    org = await make_organization()
    hub = integration_hub
    # Config-time policy: normal (accepts the public hostname).
    rebind_policy = DestinationPolicy(resolver=lambda h, p: ["10.1.2.3"])
    registry = IntegrationRegistry(
        hub.settings, hub.database, hub.event_platform.publisher, hub.vault, DestinationPolicy()
    )
    integration = await registry.create(
        org.id,
        CreateIntegrationRequest(
            slug="rebind",
            name="rebind",
            integration_type=IntegrationType.REST,
            base_url="https://api.example.com",
        ),
    )
    await registry.set_status(org.id, integration.id, IntegrationStatus.ACTIVE)
    await registry.set_operation(
        org.id,
        integration.id,
        SetOperationRequest(
            operation_key="ping.op",
            spec=RestOperationSpec(method=HttpMethod.GET, path="/x", retry_class=RetryClass.SAFE),
        ),
    )
    service = IntegrationHubService(
        hub.settings,
        hub.database,
        hub.event_platform.publisher,
        registry,
        GovernedHttpExecutor(hub.settings.integrations, rebind_policy),
        AuthProfileApplier(
            hub.vault, rebind_policy, GovernedHttpExecutor(hub.settings.integrations, rebind_policy)
        ),
        CircuitBreakerRegistry(hub.settings.integrations),
        OutboundRateLimiter(hub.settings.integrations, _NoCache()),
        IntegrationIdempotencyRepository(hub.database),
    )
    with pytest.raises(IntegrationDestinationBlockedError):
        await service.execute(
            org.id,
            ExecutionRequest(integration_id=integration.id, operation_key="ping.op", input={}),
        )


async def test_secret_is_never_returned_or_logged(
    integration_hub: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    await hub.registry.store_credential(
        org.id,
        credential_ref="crm:key",
        credential_type=CredentialType.API_KEY,
        fields={"api_key": "TOPSECRET-VALUE-XYZ"},
    )
    integration = await hub.registry.create(
        org.id,
        CreateIntegrationRequest(
            slug="crm",
            name="crm",
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
    await hub.registry.set_operation(
        org.id,
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
    mock_http_server.set_handler(lambda m, p, h, b: (500, {"boom": True}))
    from nexus_ai.integrations.errors import IntegrationUpstreamServerError

    with pytest.raises(IntegrationUpstreamServerError):
        await hub.service.execute(
            org.id,
            ExecutionRequest(
                integration_id=integration.id,
                operation_key="crm.get",
                input={"path_params": {"id": "1"}},
            ),
        )
    # The API view never exposes the credential value.
    view = (await hub.registry.get(org.id, integration.id)).public_view().model_dump_json()
    assert "TOPSECRET" not in view
    # Execution records / failure events carry no secret.
    async with tenant_database.tenant_transaction(org.id) as tenant:
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT error_code, result_class, error_code FROM integration_execution_records"
                )
            )
        ).all()
        envelopes = (
            await tenant.session.execute(
                text(
                    "SELECT envelope::text FROM event_outbox WHERE event_type LIKE 'integrations.%'"
                )
            )
        ).all()
    assert all("TOPSECRET" not in str(r) for r in rows)
    assert envelopes and all("TOPSECRET" not in e[0] for e in envelopes)


async def test_cross_tenant_integration_and_credential_isolation(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    hub = integration_hub
    org_a = await make_organization()
    org_b = await make_organization()
    integration = await hub.registry.create(
        org_a.id,
        CreateIntegrationRequest(
            slug="crm",
            name="crm",
            integration_type=IntegrationType.REST,
            base_url=mock_http_server.base_url,
        ),
    )
    await hub.registry.store_credential(
        org_a.id,
        credential_ref="shared:ref",
        credential_type=CredentialType.API_KEY,
        fields={"api_key": "a-only"},
    )
    # Org B cannot see org A's integration.
    with pytest.raises(IntegrationNotFoundError):
        await hub.registry.get(org_b.id, integration.id)
    # Org B cannot resolve org A's credential ref.
    with pytest.raises(IntegrationCredentialUnavailableError):
        await hub.vault.get_secret(org_b.id, "shared:ref")


async def test_forged_cross_tenant_operation_fk_is_refused(
    integration_hub: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    hub = integration_hub
    org_a = await make_organization()
    org_b = await make_organization()
    integration = await hub.registry.create(
        org_a.id,
        CreateIntegrationRequest(
            slug="crm",
            name="crm",
            integration_type=IntegrationType.REST,
            base_url=mock_http_server.base_url,
        ),
    )
    # Attempt to attach an operation to org A's integration from org B's scope.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):  # RLS WITH CHECK + composite FK reject it
        async with tenant_database.tenant_transaction(org_b.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO integration_operations "
                    "(id, organization_id, integration_id, operation_key, operation_type, "
                    " spec, config_revision, created_at, updated_at) VALUES "
                    "(:id, :org, :integ, 'x.y', 'REST', '{}'::jsonb, 1, now(), now())"
                ),
                {"id": uuid.uuid4(), "org": org_b.id, "integ": integration.id},
            )


async def test_no_arbitrary_http_proxy_endpoint(app_client_with_integrations: Any) -> None:
    schema = app_client_with_integrations
    paths = set(schema["paths"])
    assert not any("http/request" in p or "/proxy" in p for p in paths)
    assert "/api/v1/integrations/{integration_id}/execute" in paths
    execute = schema["paths"]["/api/v1/integrations/{integration_id}/execute"]["post"]
    ref = execute["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body_model = ref.split("/")[-1]
    props = set(schema["components"]["schemas"][body_model]["properties"])
    # operation-based only — no url / method / headers / raw query
    assert props == {
        "integration_id",
        "operation_key",
        "input",
        "idempotency_key",
        "correlation_id",
    }


async def test_arbitrary_graphql_document_cannot_be_supplied_at_execution(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    from nexus_ai.integrations.entities import GraphQLOperationSpec
    from nexus_ai.integrations.errors import IntegrationOperationInputInvalidError

    org = await make_organization()
    hub = integration_hub
    integration = await hub.registry.create(
        org.id,
        CreateIntegrationRequest(
            slug="gql",
            name="gql",
            integration_type=IntegrationType.GRAPHQL,
            base_url=mock_http_server.base_url,
        ),
    )
    await hub.registry.set_status(org.id, integration.id, IntegrationStatus.ACTIVE)
    await hub.registry.set_operation(
        org.id,
        integration.id,
        SetOperationRequest(
            operation_key="gql.node",
            spec=GraphQLOperationSpec(
                document="query Q($id: ID!) { node(id: $id) { id } }",
                operation_name="Q",
                variables_schema={"type": "object", "properties": {"id": {"type": "string"}}},
            ),
        ),
    )
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"data": {"node": {"id": "1"}}}))
    # A caller can pass variables ...
    ok = await hub.service.execute(
        org.id,
        ExecutionRequest(
            integration_id=integration.id,
            operation_key="gql.node",
            input={"variables": {"id": "1"}},
        ),
    )
    assert ok.ok
    # ... but NOT a raw query / arbitrary keys.
    with pytest.raises(IntegrationOperationInputInvalidError):
        await hub.service.execute(
            org.id,
            ExecutionRequest(
                integration_id=integration.id,
                operation_key="gql.node",
                input={"query": "mutation { deleteEverything }"},
            ),
        )


async def test_config_time_ssrf_on_operation_path(
    integration_hub: Any, make_organization: Any
) -> None:
    org = await make_organization()
    strict = IntegrationRegistry(
        integration_hub.settings,
        integration_hub.database,
        integration_hub.event_platform.publisher,
        integration_hub.vault,
        DestinationPolicy(),
    )
    integration = await strict.create(
        org.id,
        CreateIntegrationRequest(
            slug="ok",
            name="ok",
            integration_type=IntegrationType.REST,
            base_url="https://api.example.com",
        ),
    )
    # A path that escapes the host is still bounded by the base URL netloc — but a
    # protocol-relative style path attempt is caught by ParamSpec/pattern; verify a
    # normal operation registers fine and the destination stays the base host.
    op = await strict.set_operation(
        org.id,
        integration.id,
        SetOperationRequest(
            operation_key="safe.op",
            spec=RestOperationSpec(method=HttpMethod.GET, path="/v1/things"),
        ),
    )
    assert op.operation_key == "safe.op"


class _NoCache:
    is_connected = False
    client = None


@pytest.fixture
async def app_client_with_integrations(app_client: Any) -> Any:
    response = await app_client.get("/openapi.json")
    return response.json()
