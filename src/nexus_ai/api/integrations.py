"""Integration Hub API surface (NXS-INT-001).

Operation-based and deny-by-default. There is deliberately NO generic
``POST /api/v1/http/request``, no arbitrary URL proxy, no raw-header forwarding and no
arbitrary GraphQL executor: a caller chooses ``integration_id`` + ``operation_key`` +
validated ``input`` and nothing else. The inbound webhook route is the one unauthenticated
endpoint — it is identified by an unguessable token and signature-verified.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from nexus_ai.api.authorization import (
    IntegrationCreateDep,
    IntegrationCredentialManageDep,
    IntegrationDisableDep,
    IntegrationExecuteDep,
    IntegrationOperationManageDep,
    IntegrationReadDep,
    IntegrationTestDep,
    IntegrationUpdateDep,
    IntegrationWebhookManageDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.integrations.credentials import CredentialType
from nexus_ai.integrations.entities import (
    CreateIntegrationRequest,
    ExecutionRequest,
    IntegrationOperationView,
    IntegrationStatus,
    IntegrationView,
    SetOperationRequest,
    UpdateIntegrationRequest,
    WebhookSignatureScheme,
)

integrations_router = APIRouter(prefix="/integrations", tags=["integrations"])

_Limit = Annotated[int, Query(ge=1, le=100)]
_CredRef = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:-]{2,126}$")]


class StoreCredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    credential_ref: _CredRef
    credential_type: CredentialType
    fields: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,31}$")],
        Annotated[str, StringConstraints(min_length=1, max_length=4096)],
    ]


class RegisterWebhookRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
    event_type: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{1,62}$")]
    signature_scheme: WebhookSignatureScheme = WebhookSignatureScheme.NONE
    signature_header: Annotated[str, StringConstraints(max_length=64)] | None = None
    timestamp_header: Annotated[str, StringConstraints(max_length=64)] | None = None
    tolerance_seconds: Annotated[int, Field(ge=30, le=3600)] = 300
    credential_ref: _CredRef | None = None


class WebhookEndpointView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    integration_id: UUID
    slug: str
    event_type: str
    signature_scheme: WebhookSignatureScheme
    #: The full inbound URL path a provider should POST to.
    receive_path: str


class OpenApiImportView(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    version: str
    server_url: str | None
    warnings: list[str]
    operations: list[dict[str, Any]]


# --- integrations -----------------------------------------------------------------


@integrations_router.post("", response_model=IntegrationView, status_code=status.HTTP_201_CREATED)
async def create_integration(
    payload: CreateIntegrationRequest,
    context: TenantContextDep,
    _authorized: IntegrationCreateDep,
    request: Request,
) -> IntegrationView:
    integration = await get_resources(request).integrations.create(context.organization_id, payload)
    return integration.public_view()


@integrations_router.get("", response_model=list[IntegrationView])
async def list_integrations(
    context: TenantContextDep,
    _authorized: IntegrationReadDep,
    request: Request,
    limit: _Limit = 20,
    after_id: UUID | None = None,
) -> list[IntegrationView]:
    rows = await get_resources(request).integrations.list_integrations(
        context.organization_id, limit=limit, after_id=after_id
    )
    return [row.public_view() for row in rows]


@integrations_router.get("/{integration_id}", response_model=IntegrationView)
async def get_integration(
    integration_id: UUID,
    context: TenantContextDep,
    _authorized: IntegrationReadDep,
    request: Request,
) -> IntegrationView:
    integration = await get_resources(request).integrations.get(
        context.organization_id, integration_id
    )
    return integration.public_view()


@integrations_router.patch("/{integration_id}", response_model=IntegrationView)
async def update_integration(
    integration_id: UUID,
    payload: UpdateIntegrationRequest,
    context: TenantContextDep,
    _authorized: IntegrationUpdateDep,
    request: Request,
) -> IntegrationView:
    integration = await get_resources(request).integrations.update(
        context.organization_id, integration_id, payload
    )
    return integration.public_view()


@integrations_router.post("/{integration_id}/enable", response_model=IntegrationView)
async def enable_integration(
    integration_id: UUID,
    context: TenantContextDep,
    _authorized: IntegrationDisableDep,
    request: Request,
) -> IntegrationView:
    integration = await get_resources(request).integrations.set_status(
        context.organization_id, integration_id, IntegrationStatus.ACTIVE
    )
    return integration.public_view()


@integrations_router.post("/{integration_id}/disable", response_model=IntegrationView)
async def disable_integration(
    integration_id: UUID,
    context: TenantContextDep,
    _authorized: IntegrationDisableDep,
    request: Request,
) -> IntegrationView:
    integration = await get_resources(request).integrations.set_status(
        context.organization_id, integration_id, IntegrationStatus.DISABLED
    )
    return integration.public_view()


# --- operations -------------------------------------------------------------------


@integrations_router.get(
    "/{integration_id}/operations", response_model=list[IntegrationOperationView]
)
async def list_operations(
    integration_id: UUID,
    context: TenantContextDep,
    _authorized: IntegrationReadDep,
    request: Request,
) -> list[IntegrationOperationView]:
    operations = await get_resources(request).integrations.list_operations(
        context.organization_id, integration_id
    )
    return [op.public_view() for op in operations]


@integrations_router.put(
    "/{integration_id}/operations",
    response_model=IntegrationOperationView,
    status_code=status.HTTP_201_CREATED,
)
async def set_operation(
    integration_id: UUID,
    payload: SetOperationRequest,
    context: TenantContextDep,
    _authorized: IntegrationOperationManageDep,
    request: Request,
) -> IntegrationOperationView:
    operation = await get_resources(request).integrations.set_operation(
        context.organization_id, integration_id, payload
    )
    return operation.public_view()


@integrations_router.delete(
    "/{integration_id}/operations/{operation_key}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_operation(
    integration_id: UUID,
    operation_key: str,
    context: TenantContextDep,
    _authorized: IntegrationOperationManageDep,
    request: Request,
) -> Response:
    await get_resources(request).integrations.remove_operation(
        context.organization_id, integration_id, operation_key
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- credentials -----------------------------------------------------------------


@integrations_router.post("/{integration_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def store_credential(
    integration_id: UUID,
    payload: StoreCredentialRequest,
    context: TenantContextDep,
    _authorized: IntegrationCredentialManageDep,
    request: Request,
) -> Response:
    # integration_id scopes the route for RBAC; credentials are org-scoped by ref.
    await get_resources(request).integrations.get(context.organization_id, integration_id)
    await get_resources(request).integrations.store_credential(
        context.organization_id,
        credential_ref=payload.credential_ref,
        credential_type=payload.credential_type,
        fields=dict(payload.fields),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@integrations_router.delete(
    "/{integration_id}/credentials/{credential_ref}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_credential(
    integration_id: UUID,
    credential_ref: str,
    context: TenantContextDep,
    _authorized: IntegrationCredentialManageDep,
    request: Request,
) -> Response:
    await get_resources(request).integrations.get(context.organization_id, integration_id)
    await get_resources(request).integrations.delete_credential(
        context.organization_id, credential_ref
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- execution -------------------------------------------------------------------


@integrations_router.post("/{integration_id}/execute")
async def execute_operation(
    integration_id: UUID,
    payload: ExecutionRequest,
    context: TenantContextDep,
    _authorized: IntegrationExecuteDep,
    request: Request,
) -> Any:
    if payload.integration_id != integration_id:
        from nexus_ai.integrations.errors import IntegrationConfigInvalidError

        raise IntegrationConfigInvalidError("the path and body integration_id disagree")
    result = await get_resources(request).integration_hub.execute(context.organization_id, payload)
    return result.model_dump(mode="json")


@integrations_router.post("/{integration_id}/test")
async def test_operation(
    integration_id: UUID,
    payload: ExecutionRequest,
    context: TenantContextDep,
    _authorized: IntegrationTestDep,
    request: Request,
) -> Any:
    if payload.integration_id != integration_id:
        from nexus_ai.integrations.errors import IntegrationConfigInvalidError

        raise IntegrationConfigInvalidError("the path and body integration_id disagree")
    result = await get_resources(request).integration_hub.execute(
        context.organization_id, payload, is_test=True
    )
    return result.model_dump(mode="json")


# --- openapi import -------------------------------------------------------------


@integrations_router.post("/openapi/import", response_model=OpenApiImportView)
async def import_openapi(
    context: TenantContextDep,
    _authorized: IntegrationCreateDep,
    request: Request,
) -> OpenApiImportView:
    raw = await request.body()
    result = get_resources(request).integrations.import_openapi(context.organization_id, raw)
    return OpenApiImportView(
        title=result.title,
        version=result.version,
        server_url=result.server_url,
        warnings=result.warnings,
        operations=[
            {
                "operation_key": op.operation_key,
                "method": op.method.value,
                "path": op.path,
                "summary": op.summary,
                "spec": op.spec.model_dump(mode="json"),
            }
            for op in result.operations
        ],
    )


# --- webhooks ------------------------------------------------------------------


@integrations_router.post(
    "/{integration_id}/webhooks",
    response_model=WebhookEndpointView,
    status_code=status.HTTP_201_CREATED,
)
async def register_webhook(
    integration_id: UUID,
    payload: RegisterWebhookRequest,
    context: TenantContextDep,
    _authorized: IntegrationWebhookManageDep,
    request: Request,
) -> WebhookEndpointView:
    endpoint = await get_resources(request).integrations.register_webhook(
        context.organization_id,
        integration_id,
        slug=payload.slug,
        event_type=payload.event_type,
        signature_scheme=payload.signature_scheme,
        signature_header=payload.signature_header,
        timestamp_header=payload.timestamp_header,
        tolerance_seconds=payload.tolerance_seconds,
        credential_ref=payload.credential_ref,
    )
    return WebhookEndpointView(
        id=endpoint.id,
        integration_id=endpoint.integration_id,
        slug=endpoint.slug,
        event_type=endpoint.event_type,
        signature_scheme=endpoint.signature_scheme,
        receive_path=f"/api/v1/integrations/webhooks/{endpoint.public_token}",
    )


@integrations_router.post("/webhooks/{token}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(token: str, request: Request) -> dict[str, Any]:
    body = await request.body()
    acceptance = await get_resources(request).inbound_webhooks.receive(
        token, dict(request.headers), body
    )
    return {
        "accepted": True,
        "replayed": acceptance.replayed,
        "event_type": acceptance.event_type,
        "external_id": acceptance.external_id,
    }
