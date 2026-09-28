"""P06-owned bounded profile operations, not whole-customer erasure or send authority."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from nexus_ai.core.errors import NotFoundError, OrganizationVersionConflictError
from nexus_ai.domain.customers.models import CustomerRecord
from nexus_ai.infrastructure.tenant_session import TenantSession


class CustomerProfileAdapter:
    async def locked_target(self, tenant: TenantSession, identity: UUID) -> CustomerRecord:
        row = await tenant.session.scalar(
            select(CustomerRecord).where(CustomerRecord.id == identity).with_for_update()
        )
        if row is None:
            raise NotFoundError("customer profile not found in this Organization")
        return row

    def export(self, row: CustomerRecord) -> dict[str, Any]:
        return {
            "customer_id": str(row.id),
            "display_name": row.display_name,
            "preferred_locale": row.preferred_locale,
            "profile_status": row.status,
            "version": row.version,
        }

    def anonymize(self, row: CustomerRecord, expected_version: int) -> dict[str, Any]:
        self._version(row, expected_version)
        row.display_name = "Anonymized profile"
        row.preferred_locale = None
        row.version += 1
        return {"profile_anonymized": True, "whole_subject_erased": False}

    def restrict(self, row: CustomerRecord, expected_version: int) -> dict[str, Any]:
        self._version(row, expected_version)
        row.status = "SUSPENDED"
        row.version += 1
        return {"profile_suspended": True, "campaign_send_restricted": False}

    @staticmethod
    def _version(row: CustomerRecord, expected_version: int) -> None:
        if row.version != expected_version:
            raise OrganizationVersionConflictError("customer profile version changed")
