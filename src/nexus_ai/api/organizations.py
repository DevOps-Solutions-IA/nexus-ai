"""Current-Organization API (NXS-ORG-002, section 42).

There is deliberately no ``GET /organizations`` and no ``GET /organizations/{id}`` for
unauthenticated callers — platform Organization administration belongs after P03
authorization. These endpoints derive the Organization strictly from the trusted
``TenantContextResolver``; in production/staging that resolver returns nothing until P03
supplies authenticated identity, so the endpoints answer ``403 NXS_TENANT_CONTEXT_REQUIRED``.
"""

from __future__ import annotations

from fastapi import APIRouter

from nexus_ai.api.dependencies import OrganizationServiceDep, TenantContextDep
from nexus_ai.domain.organizations.entities import Organization, OrganizationProfileUpdate
from nexus_ai.domain.organizations.entities import OrganizationView as OrganizationView

organizations_router = APIRouter(prefix="/organizations", tags=["organizations"])


@organizations_router.get(
    "/current",
    response_model=OrganizationView,
    summary="The caller's current Organization",
    responses={403: {"description": "No trusted tenant context"}},
)
async def read_current_organization(
    context: TenantContextDep, service: OrganizationServiceDep
) -> OrganizationView:
    organization: Organization = await service.get_current(context)
    return organization.public_view()


@organizations_router.patch(
    "/current",
    response_model=OrganizationView,
    summary="Update the current Organization profile",
    responses={
        403: {"description": "No trusted tenant context or Organization not active"},
        409: {"description": "Version conflict"},
    },
)
async def update_current_organization(
    payload: OrganizationProfileUpdate,
    context: TenantContextDep,
    service: OrganizationServiceDep,
) -> OrganizationView:
    organization = await service.update_profile(context, payload)
    return organization.public_view()
