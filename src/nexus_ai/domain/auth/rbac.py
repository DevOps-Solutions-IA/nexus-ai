"""Organization-scoped RBAC (NXS-AUTH-006).

Permissions are stable identifiers from the global catalog; roles are global catalog
definitions; role ASSIGNMENTS are tenant-owned rows. Authorization always resolves
through assignments in the caller's Organization scope — deny by default, never by
email/domain/name heuristics, and a role granted in one Organization grants nothing in
another (RLS on the assignments table makes that a database guarantee).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import text

from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession


class PermissionKey(StrEnum):
    ORGANIZATION_READ = "organization:read"
    ORGANIZATION_WRITE = "organization:write"
    MEMBERSHIP_MANAGE = "membership:manage"
    ROLE_ASSIGN = "role:assign"
    SESSION_READ = "auth:session:read"
    # P05 additions (seeded by the P05 migration):
    ORGANIZATION_CREATE = "organization:create"  # PLATFORM capability (platform_grants)
    PROVISION_READ = "organization:provision:read"
    SETTINGS_READ = "organization:settings:read"
    DASHBOARD_READ = "dashboard:read"
    # P06 additions (seeded by the P06 migration):
    CUSTOMER_READ = "customer:read"
    CUSTOMER_CREATE = "customer:create"
    CUSTOMER_UPDATE = "customer:update"
    CUSTOMER_IDENTITY_LINK = "customer:identity:link"
    CONVERSATION_READ = "conversation:read"
    CONVERSATION_CREATE = "conversation:create"
    CONVERSATION_CLOSE = "conversation:close"
    CUSTOMER_TIMELINE_READ = "customer:timeline:read"
    # P07 additions (seeded by the P07 migration):
    INTEGRATION_READ = "integration:read"
    INTEGRATION_CREATE = "integration:create"
    INTEGRATION_UPDATE = "integration:update"
    INTEGRATION_DISABLE = "integration:disable"
    INTEGRATION_OPERATION_MANAGE = "integration:operation:manage"
    INTEGRATION_CREDENTIAL_MANAGE = "integration:credential:manage"
    INTEGRATION_TEST = "integration:test"
    INTEGRATION_EXECUTE = "integration:execute"
    INTEGRATION_WEBHOOK_MANAGE = "integration:webhook:manage"
    # P08 additions (seeded by the P08 migration):
    TOOL_READ = "tool:read"
    TOOL_CREATE = "tool:create"
    TOOL_UPDATE = "tool:update"
    TOOL_DISABLE = "tool:disable"
    TOOL_INVOKE = "tool:invoke"
    # P09 additions (seeded by the P09 migration):
    MESSAGING_READ = "messaging:read"
    MESSAGING_SEND = "messaging:send"
    MESSAGING_MANAGE_ACCOUNTS = "messaging:manage_accounts"
    # P10 additions (seeded by the P10 migration):
    OTP_READ = "otp:read"
    OTP_ISSUE = "otp:issue"
    OTP_VERIFY = "otp:verify"
    # P11 additions (seeded by the P11 migration):
    TELEPHONY_READ = "telephony:read"
    TELEPHONY_CALL = "telephony:call"
    TELEPHONY_HANGUP = "telephony:hangup"
    TELEPHONY_CONFIGURE = "telephony:configure"


class RoleKey(StrEnum):
    ORG_OWNER = "org_owner"
    ORG_ADMIN = "org_admin"
    ORG_MEMBER = "org_member"


#: Permission sets granted by each built-in role, seeded into the global catalog by the
#: P03 migration — FROZEN at the P03-era mapping: the migration reads this constant at
#: runtime, so extending it here would retroactively change P03's historical seed. The
#: P05 migration (04a4640c5b95) seeds the extended grants as explicit delta rows:
#: org_owner/org_admin/org_member additionally hold organization:provision:read,
#: organization:settings:read and dashboard:read. ``organization:create`` is
#: deliberately a PLATFORM capability granted through ``platform_grants`` — no
#: Organization role ever grants it.
ROLE_PERMISSIONS: Final[dict[RoleKey, tuple[PermissionKey, ...]]] = {
    RoleKey.ORG_OWNER: (
        PermissionKey.ORGANIZATION_READ,
        PermissionKey.ORGANIZATION_WRITE,
        PermissionKey.MEMBERSHIP_MANAGE,
        PermissionKey.ROLE_ASSIGN,
        PermissionKey.SESSION_READ,
    ),
    RoleKey.ORG_ADMIN: (
        PermissionKey.ORGANIZATION_READ,
        PermissionKey.ORGANIZATION_WRITE,
        PermissionKey.MEMBERSHIP_MANAGE,
        PermissionKey.SESSION_READ,
    ),
    RoleKey.ORG_MEMBER: (
        PermissionKey.ORGANIZATION_READ,
        PermissionKey.SESSION_READ,
    ),
}

#: Deterministic catalog ids, seeded by the P03 migration so every environment agrees.
ROLE_IDS: Final[dict[RoleKey, UUID]] = {
    RoleKey.ORG_OWNER: UUID("a1000000-0000-7000-8000-000000000001"),
    RoleKey.ORG_ADMIN: UUID("a1000000-0000-7000-8000-000000000002"),
    RoleKey.ORG_MEMBER: UUID("a1000000-0000-7000-8000-000000000003"),
}
PERMISSION_IDS: Final[dict[PermissionKey, UUID]] = {
    PermissionKey.ORGANIZATION_READ: UUID("b2000000-0000-7000-8000-000000000001"),
    PermissionKey.ORGANIZATION_WRITE: UUID("b2000000-0000-7000-8000-000000000002"),
    PermissionKey.MEMBERSHIP_MANAGE: UUID("b2000000-0000-7000-8000-000000000003"),
    PermissionKey.ROLE_ASSIGN: UUID("b2000000-0000-7000-8000-000000000004"),
    PermissionKey.SESSION_READ: UUID("b2000000-0000-7000-8000-000000000005"),
    PermissionKey.ORGANIZATION_CREATE: UUID("b2000000-0000-7000-8000-000000000006"),
    PermissionKey.PROVISION_READ: UUID("b2000000-0000-7000-8000-000000000007"),
    PermissionKey.SETTINGS_READ: UUID("b2000000-0000-7000-8000-000000000008"),
    PermissionKey.DASHBOARD_READ: UUID("b2000000-0000-7000-8000-000000000009"),
    PermissionKey.CUSTOMER_READ: UUID("b2000000-0000-7000-8000-00000000000a"),
    PermissionKey.CUSTOMER_CREATE: UUID("b2000000-0000-7000-8000-00000000000b"),
    PermissionKey.CUSTOMER_UPDATE: UUID("b2000000-0000-7000-8000-00000000000c"),
    PermissionKey.CUSTOMER_IDENTITY_LINK: UUID("b2000000-0000-7000-8000-00000000000d"),
    PermissionKey.CONVERSATION_READ: UUID("b2000000-0000-7000-8000-00000000000e"),
    PermissionKey.CONVERSATION_CREATE: UUID("b2000000-0000-7000-8000-00000000000f"),
    PermissionKey.CONVERSATION_CLOSE: UUID("b2000000-0000-7000-8000-000000000010"),
    PermissionKey.CUSTOMER_TIMELINE_READ: UUID("b2000000-0000-7000-8000-000000000011"),
    PermissionKey.INTEGRATION_READ: UUID("b2000000-0000-7000-8000-000000000012"),
    PermissionKey.INTEGRATION_CREATE: UUID("b2000000-0000-7000-8000-000000000013"),
    PermissionKey.INTEGRATION_UPDATE: UUID("b2000000-0000-7000-8000-000000000014"),
    PermissionKey.INTEGRATION_DISABLE: UUID("b2000000-0000-7000-8000-000000000015"),
    PermissionKey.INTEGRATION_OPERATION_MANAGE: UUID("b2000000-0000-7000-8000-000000000016"),
    PermissionKey.INTEGRATION_CREDENTIAL_MANAGE: UUID("b2000000-0000-7000-8000-000000000017"),
    PermissionKey.INTEGRATION_TEST: UUID("b2000000-0000-7000-8000-000000000018"),
    PermissionKey.INTEGRATION_EXECUTE: UUID("b2000000-0000-7000-8000-000000000019"),
    PermissionKey.INTEGRATION_WEBHOOK_MANAGE: UUID("b2000000-0000-7000-8000-00000000001a"),
    PermissionKey.TOOL_READ: UUID("b2000000-0000-7000-8000-00000000001b"),
    PermissionKey.TOOL_CREATE: UUID("b2000000-0000-7000-8000-00000000001c"),
    PermissionKey.TOOL_UPDATE: UUID("b2000000-0000-7000-8000-00000000001d"),
    PermissionKey.TOOL_DISABLE: UUID("b2000000-0000-7000-8000-00000000001e"),
    PermissionKey.TOOL_INVOKE: UUID("b2000000-0000-7000-8000-00000000001f"),
    PermissionKey.MESSAGING_READ: UUID("b2000000-0000-7000-8000-000000000020"),
    PermissionKey.MESSAGING_SEND: UUID("b2000000-0000-7000-8000-000000000021"),
    PermissionKey.MESSAGING_MANAGE_ACCOUNTS: UUID("b2000000-0000-7000-8000-000000000022"),
    PermissionKey.OTP_READ: UUID("b2000000-0000-7000-8000-000000000023"),
    PermissionKey.OTP_ISSUE: UUID("b2000000-0000-7000-8000-000000000024"),
    PermissionKey.OTP_VERIFY: UUID("b2000000-0000-7000-8000-000000000025"),
    PermissionKey.TELEPHONY_READ: UUID("b2000000-0000-7000-8000-000000000026"),
    PermissionKey.TELEPHONY_CALL: UUID("b2000000-0000-7000-8000-000000000027"),
    PermissionKey.TELEPHONY_HANGUP: UUID("b2000000-0000-7000-8000-000000000028"),
    PermissionKey.TELEPHONY_CONFIGURE: UUID("b2000000-0000-7000-8000-000000000029"),
}

_CATALOG_SQL = """
SELECT p.permission_key
FROM role_assignments ra
JOIN roles r ON r.id = ra.role_id
JOIN role_permissions rp ON rp.role_id = r.id
JOIN permissions p ON p.id = rp.permission_id
WHERE ra.user_id = :user_id AND ra.status = 'ACTIVE'
"""


class AuthorizationService:
    """Deny-by-default permission boundary. Every check is explicit and org-scoped."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._logger = get_logger("nexus_ai.auth.rbac")

    async def permissions_for(self, tenant: TenantSession, user_id: UUID) -> set[str]:
        rows = (await tenant.session.execute(text(_CATALOG_SQL), {"user_id": user_id})).scalars()
        return {str(row) for row in rows}

    async def require(
        self, principal: Principal, permission: PermissionKey, *, tenant: TenantSession
    ) -> None:
        granted = await self.permissions_for(tenant, principal.user_id)
        if permission.value not in granted:
            await self._logger.ainfo(
                "auth_authorization_denied",
                user_id=str(principal.user_id),
                organization_id=str(principal.organization_id),
                permission=permission.value,
            )
            raise PermissionDeniedError(
                f"The caller lacks the {permission.value!r} permission in this Organization.",
                extensions={"permission": permission.value},
            )

    async def has_permission(
        self, principal: Principal, permission: PermissionKey, *, tenant: TenantSession
    ) -> bool:
        granted = await self.permissions_for(tenant, principal.user_id)
        return permission.value in granted
