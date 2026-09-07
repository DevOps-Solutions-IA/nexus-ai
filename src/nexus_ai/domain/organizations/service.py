"""Organization domain service — owns Organization rules (NXS-ORG-002, NXS-ORG-003).

SQLAlchemy details stay behind the repository. Every method runs inside a
``tenant_transaction`` so the tenant GUC is bound for the whole unit of work. This is
NOT the P05 provisioner: ``create_core_record`` creates only the core row in
``PROVISIONING`` — no admin, RBAC, quotas, channels, dashboards or billing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from uuid import UUID

from nexus_ai.core.errors import OrganizationInactiveError
from nexus_ai.core.tenancy import TenantContext
from nexus_ai.domain.organizations.entities import (
    Organization,
    OrganizationDraft,
    OrganizationProfileUpdate,
)
from nexus_ai.domain.organizations.repository import OrganizationRepository
from nexus_ai.domain.organizations.status import (
    OrganizationStatus,
    assert_transition,
    is_operational,
)
from nexus_ai.infrastructure.database import Database

_TRANSITION_TIMESTAMP: dict[OrganizationStatus, str] = {
    OrganizationStatus.ACTIVE: "activated_at",
    OrganizationStatus.SUSPENDED: "suspended_at",
    OrganizationStatus.ARCHIVED: "archived_at",
}


class OrganizationService:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def create_core_record(self, draft: OrganizationDraft) -> Organization:
        """Create the core Organization row (PROVISIONING). Id is server-generated UUIDv7."""
        organization_id = uuid.uuid7()
        async with self._db.tenant_transaction(organization_id) as tenant_session:
            repository = OrganizationRepository(tenant_session)
            return await repository.insert(draft, organization_id=organization_id)

    async def get_current(self, context: TenantContext) -> Organization:
        async with self._db.tenant_transaction(context.organization_id) as tenant_session:
            return await OrganizationRepository(tenant_session).get_current()

    async def require_operational(self, context: TenantContext) -> Organization:
        organization = await self.get_current(context)
        if not is_operational(organization.status):
            raise OrganizationInactiveError(
                "The Organization is not accepting normal operations.",
                extensions={"status": organization.status.value},
            )
        return organization

    async def update_profile(
        self, context: TenantContext, payload: OrganizationProfileUpdate
    ) -> Organization:
        async with self._db.tenant_transaction(context.organization_id) as tenant_session:
            repository = OrganizationRepository(tenant_session)
            current = await repository.get_current()
            if not is_operational(current.status):
                raise OrganizationInactiveError(
                    "Profile changes require an ACTIVE Organization.",
                    extensions={"status": current.status.value},
                )
            return await repository.apply(
                expected_version=payload.expected_version, changes=payload.changes()
            )

    async def transition(self, organization_id: UUID, target: OrganizationStatus) -> Organization:
        """Legal lifecycle transition. A system/service operation (used by tests and P05)."""
        async with self._db.tenant_transaction(organization_id) as tenant_session:
            repository = OrganizationRepository(tenant_session)
            current = await repository.get_current()
            assert_transition(current.status, target)
            changes: dict[str, object] = {"status": target.value}
            timestamp_column = _TRANSITION_TIMESTAMP.get(target)
            if timestamp_column is not None:
                changes[timestamp_column] = dt.datetime.now(dt.UTC)
            return await repository.apply(expected_version=current.version, changes=changes)
