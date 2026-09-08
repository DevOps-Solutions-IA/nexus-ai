"""Server-side tool permission enforcement (NXS-TOOL-001).

A tool declares ``required_permissions`` — stable ``PermissionKey`` identifiers. At
invocation the caller principal's ACTUAL granted permissions in the bound Organization
are resolved through the canonical P03 :class:`AuthorizationService` and every required
permission must be present. A model / caller cannot assert a permission it does not hold;
the requirement list itself is validated against the known catalog at registration.

Every tool additionally requires ``integration:execute`` — a tool always ends in a
governed Integration Hub call — and ``tool:invoke``.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from nexus_ai.core.logging import get_logger
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.rbac import AuthorizationService, PermissionKey
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.tools.errors import ToolConfigInvalidError, ToolPermissionDeniedError

_KNOWN_PERMISSIONS = frozenset(p.value for p in PermissionKey)

#: implied on every tool — a tool always executes a governed integration operation.
IMPLIED_TOOL_PERMISSIONS: tuple[str, ...] = (
    PermissionKey.INTEGRATION_EXECUTE.value,
    PermissionKey.TOOL_INVOKE.value,
)


def normalise_required_permissions(declared: Sequence[str]) -> tuple[str, ...]:
    """Validate every declared permission against the catalog and fold in the implied
    set. Raises ``NXS_TOOL_CONFIG_INVALID`` for an unknown permission."""
    unknown = [p for p in declared if p not in _KNOWN_PERMISSIONS]
    if unknown:
        raise ToolConfigInvalidError(
            f"unknown permission(s) in required_permissions: {', '.join(sorted(unknown))}"
        )
    return tuple(sorted({*declared, *IMPLIED_TOOL_PERMISSIONS}))


class ToolPermissionGuard:
    def __init__(self, authorizer: AuthorizationService) -> None:
        self._authorizer = authorizer
        self._log = get_logger("nexus_ai.tools.permissions")

    async def require(
        self,
        tenant: TenantSession,
        principal: Principal,
        required: Sequence[str],
    ) -> None:
        granted = await self._authorizer.permissions_for(tenant, principal.user_id)
        missing = [p for p in required if p not in granted]
        if missing:
            await self._log.ainfo(
                "tool_permission_denied",
                user_id=str(principal.user_id),
                organization_id=str(principal.organization_id),
                missing=sorted(missing),
            )
            raise ToolPermissionDeniedError(
                "the caller lacks a permission this tool requires in this Organization",
                extensions={"missing_permissions": sorted(missing)},
            )


def organization_of(principal: Principal) -> UUID:
    """The trusted Organization id — from the authenticated principal, NEVER a payload."""
    return principal.organization_id
