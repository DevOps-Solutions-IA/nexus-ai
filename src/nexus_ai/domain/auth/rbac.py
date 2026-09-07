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


class RoleKey(StrEnum):
    ORG_OWNER = "org_owner"
    ORG_ADMIN = "org_admin"
    ORG_MEMBER = "org_member"


#: Permission sets granted by each built-in role. Seeded into the global catalog by the
#: P03 migration; the database catalog is the runtime authority.
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
