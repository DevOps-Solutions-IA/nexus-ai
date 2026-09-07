"""Organization API (NXS-ORG-001/002, NXS-AUTH-006, NXS-DASH-001).

Tenant-scoped endpoints derive the Organization strictly from the trusted
``TenantContextResolver`` (cryptographically verified token + live security-state
validation) and enforce the canonical RBAC boundary. ``POST /organizations`` is the
one PLATFORM control-plane operation: it requires the ``organization:create``
platform capability (``platform_grants``) — Organization roles never grant it — and
is idempotent by contract (a required ``idempotency_key``). Unauthenticated callers
answer ``403 NXS_TENANT_CONTEXT_REQUIRED``; there is still no cross-tenant
``GET /organizations/{id}`` surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from nexus_ai.api.auth_deps import LivePrincipalDep, PrincipalDep
from nexus_ai.api.authorization import (
    DashboardReadDep,
    OrgProvisionReadDep,
    OrgReadDep,
    OrgSettingsReadDep,
    OrgWriteDep,
)
from nexus_ai.api.dependencies import OrganizationServiceDep, TenantContextDep, get_resources
from nexus_ai.domain.dashboard.schema import DashboardSchema
from nexus_ai.domain.organizations.entities import Organization, OrganizationProfileUpdate
from nexus_ai.domain.organizations.entities import OrganizationView as OrganizationView
from nexus_ai.domain.provisioning.entities import (
    OnboardingRequest,
    OrganizationSettingsView,
    ProvisioningResult,
    ProvisioningStatusView,
)

organizations_router = APIRouter(prefix="/organizations", tags=["organizations"])


@organizations_router.get(
    "/current",
    response_model=OrganizationView,
    summary="The caller's current Organization",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "No trusted tenant context or missing organization:read"},
    },
)
async def read_current_organization(
    context: TenantContextDep,
    _authorized: OrgReadDep,
    service: OrganizationServiceDep,
) -> OrganizationView:
    organization: Organization = await service.get_current(context)
    return organization.public_view()


@organizations_router.patch(
    "/current",
    response_model=OrganizationView,
    summary="Update the current Organization profile",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing organization:write or Organization not active"},
        409: {"description": "Version conflict"},
    },
)
async def update_current_organization(
    payload: OrganizationProfileUpdate,
    context: TenantContextDep,
    _authorized: OrgWriteDep,
    service: OrganizationServiceDep,
) -> OrganizationView:
    organization = await service.update_profile(context, payload)
    return organization.public_view()


@organizations_router.post(
    "",
    response_model=ProvisioningResult,
    status_code=201,
    summary="Provision a new Organization (platform capability, idempotent)",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing the organization:create platform capability"},
        409: {"description": "Idempotency conflict, duplicate key or terminal failure replay"},
        422: {"description": "Invalid onboarding input or unavailable initial owner"},
    },
)
async def create_organization(
    payload: OnboardingRequest,
    principal: LivePrincipalDep,
    request: Request,
) -> ProvisioningResult:
    resources = get_resources(request)
    caller_has_grant = await resources.provisioner.has_create_capability(principal.user_id)
    return await resources.provisioner.provision(
        payload,
        caller_user_id=principal.user_id,
        caller_has_platform_grant=caller_has_grant,
    )


@organizations_router.get(
    "/current/provisioning",
    response_model=ProvisioningStatusView,
    summary="The current Organization's provisioning workflow state",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing organization:provision:read"},
    },
)
async def read_provisioning_status(
    context: TenantContextDep,
    _authorized: OrgProvisionReadDep,
    request: Request,
) -> ProvisioningStatusView:
    return await get_resources(request).provisioner.status_for(context.organization_id)


@organizations_router.get(
    "/current/settings",
    response_model=OrganizationSettingsView,
    summary="The current Organization's platform settings",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing organization:settings:read"},
    },
)
async def read_organization_settings(
    context: TenantContextDep,
    _authorized: OrgSettingsReadDep,
    request: Request,
) -> OrganizationSettingsView:
    resources = get_resources(request)
    from nexus_ai.domain.provisioning.repository import OrganizationSettingsRepository

    async with resources.database.tenant_transaction(context.organization_id) as tenant:
        row = await OrganizationSettingsRepository(tenant).get()
    if row is None:
        from nexus_ai.core.errors import NotFoundError

        raise NotFoundError("No settings exist for this Organization.")
    return OrganizationSettingsView(
        organization_id=row.organization_id,
        locale=row.locale,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@organizations_router.get(
    "/current/dashboard-schema",
    response_model=DashboardSchema,
    summary="The current Organization's dashboard schema (permission-filtered)",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing dashboard:read"},
        409: {"description": "Unsupported schema version or unknown widget"},
    },
)
async def read_dashboard_schema(
    context: TenantContextDep,
    _authorized: DashboardReadDep,
    principal: PrincipalDep,
    request: Request,
) -> DashboardSchema:
    resources = get_resources(request)
    from nexus_ai.domain.dashboard.service import DashboardSchemaService

    async with resources.database.tenant_transaction(context.organization_id) as tenant:
        granted = await resources.authorizer.permissions_for(tenant, principal.user_id)
        return await DashboardSchemaService().for_organization(tenant, viewer_permissions=granted)
