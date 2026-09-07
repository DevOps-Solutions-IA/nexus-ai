"""Organization persistence access (NXS-ORG-002).

Tenant repository methods operate on the CURRENT Organization only — there is no
``get(arbitrary_id)``, no ``list_all`` and no cross-tenant search in P02. RLS makes the
"current" scoping a database guarantee, not a convention. ORM rows never leave this
module; callers receive the ``Organization`` domain view.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from nexus_ai.core.errors import (
    OrganizationConflictError,
    OrganizationNotFoundError,
    OrganizationVersionConflictError,
    TenantScopeMismatchError,
)
from nexus_ai.domain.organizations.entities import Organization, OrganizationDraft
from nexus_ai.domain.organizations.models import OrganizationRecord
from nexus_ai.domain.organizations.status import OrganizationStatus
from nexus_ai.infrastructure.tenant_session import TenantSession


def _to_domain(row: OrganizationRecord) -> Organization:
    return Organization(
        id=row.id,
        organization_key=row.organization_key,
        display_name=row.display_name,
        legal_name=row.legal_name,
        country_code=row.country_code,
        timezone=row.timezone,
        industry_code=row.industry_code,
        tax_identifier=row.tax_identifier,
        status=OrganizationStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        activated_at=row.activated_at,
        suspended_at=row.suspended_at,
        archived_at=row.archived_at,
    )


class OrganizationRepository:
    """Scoped to ``tenant_session.organization_id`` by Row-Level Security."""

    def __init__(self, tenant_session: TenantSession) -> None:
        self._tenant = tenant_session
        self._session = tenant_session.session

    async def get_current(self) -> Organization:
        row = (await self._session.execute(select(OrganizationRecord).limit(2))).scalars().all()
        if not row:
            raise OrganizationNotFoundError("No Organization is visible in this tenant scope.")
        if len(row) > 1:  # pragma: no cover - RLS makes this impossible
            raise TenantScopeMismatchError("Tenant scope resolved more than one Organization.")
        return _to_domain(row[0])

    async def insert(self, draft: OrganizationDraft, *, organization_id: UUID) -> Organization:
        if organization_id != self._tenant.organization_id:
            raise TenantScopeMismatchError("Bootstrap id does not match the bound tenant scope.")
        now = dt.datetime.now(dt.UTC)
        record = OrganizationRecord(
            id=organization_id,
            organization_key=draft.organization_key,
            display_name=draft.display_name,
            legal_name=draft.legal_name,
            country_code=draft.country_code,
            timezone=draft.timezone,
            industry_code=draft.industry_code,
            tax_identifier=draft.tax_identifier,
            status=OrganizationStatus.PROVISIONING.value,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise OrganizationConflictError(
                "An Organization with that key already exists.", cause=exc
            ) from exc
        await self._session.refresh(record)
        return _to_domain(record)

    async def apply(self, *, expected_version: int, changes: Mapping[str, Any]) -> Organization:
        """Optimistic-concurrency update of the current Organization."""
        values: dict[str, Any] = dict(changes)
        values["version"] = OrganizationRecord.version + 1
        values["updated_at"] = dt.datetime.now(dt.UTC)
        result = await self._session.execute(
            update(OrganizationRecord)
            .where(OrganizationRecord.version == expected_version)
            .values(**values)
            .returning(OrganizationRecord)
        )
        updated = result.scalars().one_or_none()
        if updated is not None:
            return _to_domain(updated)
        # Nothing updated: either the row is gone from scope or the version moved on.
        current = (await self._session.execute(select(OrganizationRecord))).scalars().first()
        if current is None:
            raise OrganizationNotFoundError("No Organization is visible in this tenant scope.")
        raise OrganizationVersionConflictError(
            "The Organization was modified concurrently.",
            extensions={"expected_version": expected_version, "actual_version": current.version},
        )
