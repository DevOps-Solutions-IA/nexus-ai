"""Internal membership and role administration seam (NXS-AUTH-002, NXS-AUTH-006).

No HTTP endpoints here: these operations are the authorized domain seam used by tests,
the future P05 provisioner and later administrative surfaces. Every mutating operation
either runs as a system bootstrap (``actor=None`` — the P05 provisioning path) or
requires an authenticated actor holding the relevant permission in the TARGET
Organization. Cross-tenant calls fail at the RLS layer whatever the arguments say.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from nexus_ai.core.errors import (
    MembershipInactiveError,
    MembershipRequiredError,
    PermissionDeniedError,
)
from nexus_ai.domain.auth.entities import (
    Membership,
    MembershipStatus,
    Principal,
    RoleAssignmentStatus,
)
from nexus_ai.domain.auth.rbac import AuthorizationService, PermissionKey, RoleKey
from nexus_ai.domain.auth.repository import MembershipRepository, RoleAssignmentRepository
from nexus_ai.infrastructure.database import Database


def _role_value(role: RoleKey) -> str:
    """Extract the role key robustly — a caller-supplied non-enum value must land in
    the catalog lookup and be denied, never crash on attribute access."""
    return role.value if isinstance(role, RoleKey) else str(role)


class MembershipService:
    """Authorized membership and role assignment operations for one Organization."""

    def __init__(self, database: Database, authorizer: AuthorizationService) -> None:
        self._db = database
        self._authorizer = authorizer

    async def create(
        self,
        *,
        actor: Principal | None,
        organization_id: UUID,
        user_id: UUID,
        role: RoleKey,
    ) -> Membership:
        """Create an ACTIVE membership plus its first role assignment.

        ``actor=None`` is the system bootstrap path (tests, P05 provisioner). An
        authenticated actor must hold ``membership:manage`` in the target Organization.
        """
        async with self._db.tenant_transaction(organization_id) as tenant:
            if actor is not None:
                await self._authorizer.require(
                    actor, PermissionKey.MEMBERSHIP_MANAGE, tenant=tenant
                )
            membership = await MembershipRepository(tenant).insert(
                membership_id=uuid.uuid7(), organization_id=organization_id, user_id=user_id
            )
            role_id = await RoleAssignmentRepository(tenant).role_id_by_key(_role_value(role))
            if role_id is None:
                raise PermissionDeniedError(f"Unknown role {_role_value(role)!r}.")
            await RoleAssignmentRepository(tenant).assign(
                assignment_id=uuid.uuid7(),
                organization_id=organization_id,
                user_id=user_id,
                role_id=role_id,
            )
            return membership

    async def suspend(self, *, actor: Principal, organization_id: UUID, user_id: UUID) -> None:
        await self._require_manage(actor, organization_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            membership = await MembershipRepository(tenant).for_user_in_organization(
                user_id, organization_id
            )
            if membership is None:
                raise MembershipRequiredError("The user is not a member of this Organization.")
            await MembershipRepository(tenant).set_status(membership.id, MembershipStatus.SUSPENDED)

    async def revoke(self, *, actor: Principal, organization_id: UUID, user_id: UUID) -> None:
        await self._require_manage(actor, organization_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            membership = await MembershipRepository(tenant).for_user_in_organization(
                user_id, organization_id
            )
            if membership is None:
                raise MembershipRequiredError("The user is not a member of this Organization.")
            await MembershipRepository(tenant).set_status(membership.id, MembershipStatus.REVOKED)

    async def restore(self, *, actor: Principal, organization_id: UUID, user_id: UUID) -> None:
        """Reactivate a SUSPENDED or REVOKED membership (explicit recovery semantics)."""
        await self._require_manage(actor, organization_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            membership = await MembershipRepository(tenant).for_user_in_organization(
                user_id, organization_id
            )
            if membership is None:
                raise MembershipRequiredError("The user is not a member of this Organization.")
            await MembershipRepository(tenant).set_status(membership.id, MembershipStatus.ACTIVE)

    async def assign_role(
        self,
        *,
        actor: Principal,
        organization_id: UUID,
        user_id: UUID,
        role: RoleKey,
    ) -> None:
        await self._require_manage(actor, organization_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            membership = await MembershipRepository(tenant).for_user_in_organization(
                user_id, organization_id
            )
            if membership is None or not membership.is_active:
                raise MembershipInactiveError("The user has no active membership here.")
            role_id = await RoleAssignmentRepository(tenant).role_id_by_key(_role_value(role))
            if role_id is None:
                raise PermissionDeniedError(f"Unknown role {_role_value(role)!r}.")
            await RoleAssignmentRepository(tenant).assign(
                assignment_id=uuid.uuid7(),
                organization_id=organization_id,
                user_id=user_id,
                role_id=role_id,
            )

    async def suspend_assignment(
        self,
        *,
        actor: Principal,
        organization_id: UUID,
        assignment_id: UUID,
    ) -> None:
        await self._require_manage(actor, organization_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            await RoleAssignmentRepository(tenant).set_status(
                assignment_id, RoleAssignmentStatus.SUSPENDED
            )

    async def _require_manage(self, actor: Principal, organization_id: UUID) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._authorizer.require(actor, PermissionKey.MEMBERSHIP_MANAGE, tenant=tenant)
