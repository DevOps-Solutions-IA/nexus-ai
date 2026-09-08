"""Authorization dependencies for protected endpoints (NXS-AUTH-006, audit Finding 2).

``_permission_check`` builds a dependency that resolves the authenticated principal,
opens a tenant-scoped transaction and runs the canonical deny-by-default
:class:`AuthorizationService` check inside it — the same boundary every protected
route uses, so permission logic lives in exactly one place. RLS confines the
assignment lookup to the token's Organization, so a role granted elsewhere can never
authorize here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Request

from nexus_ai.api.auth_deps import PrincipalDep
from nexus_ai.api.dependencies import get_resources
from nexus_ai.domain.auth.rbac import PermissionKey
from nexus_ai.infrastructure.tenant_session import TenantSession


def _permission_check(permission: PermissionKey) -> Callable[..., AsyncIterator[TenantSession]]:
    async def _check(request: Request, principal: PrincipalDep) -> AsyncIterator[TenantSession]:
        resources = get_resources(request)
        async with resources.database.tenant_transaction(principal.organization_id) as tenant:
            await resources.authorizer.require(principal, permission, tenant=tenant)
            yield tenant

    return _check


OrgReadDep = Annotated[TenantSession, Depends(_permission_check(PermissionKey.ORGANIZATION_READ))]
OrgWriteDep = Annotated[TenantSession, Depends(_permission_check(PermissionKey.ORGANIZATION_WRITE))]
OrgProvisionReadDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.PROVISION_READ))
]
OrgSettingsReadDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.SETTINGS_READ))
]
DashboardReadDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.DASHBOARD_READ))
]
CustomerReadDep = Annotated[TenantSession, Depends(_permission_check(PermissionKey.CUSTOMER_READ))]
CustomerCreateDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CUSTOMER_CREATE))
]
CustomerUpdateDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CUSTOMER_UPDATE))
]
CustomerIdentityLinkDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CUSTOMER_IDENTITY_LINK))
]
ConversationReadDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CONVERSATION_READ))
]
ConversationCreateDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CONVERSATION_CREATE))
]
ConversationCloseDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CONVERSATION_CLOSE))
]
TimelineReadDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.CUSTOMER_TIMELINE_READ))
]
IntegrationReadDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_READ))
]
IntegrationCreateDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_CREATE))
]
IntegrationUpdateDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_UPDATE))
]
IntegrationDisableDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_DISABLE))
]
IntegrationOperationManageDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_OPERATION_MANAGE))
]
IntegrationCredentialManageDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_CREDENTIAL_MANAGE))
]
IntegrationTestDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_TEST))
]
IntegrationExecuteDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_EXECUTE))
]
IntegrationWebhookManageDep = Annotated[
    TenantSession, Depends(_permission_check(PermissionKey.INTEGRATION_WEBHOOK_MANAGE))
]
