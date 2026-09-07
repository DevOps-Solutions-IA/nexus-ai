"""Provisioning persistence access (NXS-ORG-001, NXS-DASH-001).

Two scoping contracts, explicit in the constructor types:

* global repositories (``ProvisioningRequestRepository``, ``PlatformGrantRepository``)
  take a plain ``AsyncSession`` — the idempotency ledger and platform grants exist
  before/outside any tenant scope by classification (ADR-0050).
* tenant repositories (``OrganizationSettingsRepository``,
  ``DashboardConfigurationRepository``) take a ``TenantSession`` — every query is
  RLS-confined to the bound Organization.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.core.errors import (
    ProvisioningStateConflictError,
    UserConflictError,
)
from nexus_ai.domain.provisioning.entities import (
    ProvisioningRequestStatus,
    ProvisioningStatusView,
)
from nexus_ai.domain.provisioning.models import (
    DashboardConfigurationRecord,
    OrganizationSettingsRecord,
    PlatformGrantRecord,
    ProvisioningRequestRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession


class ProvisioningRequestRepository:
    """Global idempotency ledger. Exact-hash lookups only — never a listing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_key_hash(self, key_hash: str) -> ProvisioningRequestRecord | None:
        return (
            (
                await self._session.execute(
                    select(ProvisioningRequestRecord).where(
                        ProvisioningRequestRecord.idempotency_key_hash == key_hash
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def by_organization(self, organization_id: uuid.UUID) -> ProvisioningRequestRecord | None:
        return (
            (
                await self._session.execute(
                    select(ProvisioningRequestRecord)
                    .where(ProvisioningRequestRecord.organization_id == organization_id)
                    .order_by(ProvisioningRequestRecord.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def by_organization_key(self, organization_key: str) -> ProvisioningRequestRecord | None:
        # Prefer a request that actually completed an Organization (crash-resume
        # prefers the committed work over a bare PENDING claim), newest first.
        return (
            (
                await self._session.execute(
                    select(ProvisioningRequestRecord)
                    .where(ProvisioningRequestRecord.organization_key == organization_key)
                    .order_by(
                        ProvisioningRequestRecord.organization_id.is_(None),
                        ProvisioningRequestRecord.created_at.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def insert_pending(
        self,
        *,
        request_id: uuid.UUID,
        key_hash: str,
        fingerprint: str,
        organization_key: str,
        owner_user_id: uuid.UUID,
        created_by_user_id: uuid.UUID,
        request_payload: dict[str, Any],
    ) -> ProvisioningRequestRecord:
        record = ProvisioningRequestRecord(
            id=request_id,
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            organization_id=None,
            organization_key=organization_key,
            status=ProvisioningRequestStatus.PENDING.value,
            owner_user_id=owner_user_id,
            created_by_user_id=created_by_user_id,
            request_payload=request_payload,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # ONLY the idempotency-key uniqueness counts as a claim conflict. Any
            # other integrity violation (e.g. an unknown owner user FK) is a real
            # error and must propagate.
            if "uq_provisioning_requests_key_hash" not in str(exc.orig):
                raise
            raise _KeyHashConflict() from exc
        await self._session.refresh(record)
        return record

    async def complete(self, request_id: uuid.UUID, *, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            update(ProvisioningRequestRecord)
            .where(ProvisioningRequestRecord.id == request_id)
            .values(
                status=ProvisioningRequestStatus.COMPLETED.value,
                organization_id=organization_id,
                completed_at=dt.datetime.now(dt.UTC),
                updated_at=dt.datetime.now(dt.UTC),
            )
        )

    async def mark_failed(self, request_id: uuid.UUID, *, error_code: str) -> None:
        await self._session.execute(
            update(ProvisioningRequestRecord)
            .where(ProvisioningRequestRecord.id == request_id)
            .values(
                status=ProvisioningRequestStatus.FAILED.value,
                error_code=error_code[:64],
                completed_at=dt.datetime.now(dt.UTC),
                updated_at=dt.datetime.now(dt.UTC),
            )
        )

    def to_status_view(self, row: ProvisioningRequestRecord) -> ProvisioningStatusView:
        if row.organization_id is None:
            raise ValueError("a provisioning request without an Organization has no status view")
        return ProvisioningStatusView(
            organization_id=row.organization_id,
            organization_key=row.organization_key,
            status=ProvisioningRequestStatus(row.status),
            error_code=row.error_code,
            requested_at=row.created_at,
            completed_at=row.completed_at,
        )


class _KeyHashConflict(Exception):
    """Internal signal: the idempotency key hash already exists."""


class PlatformGrantRepository:
    """Global platform capability grants. Explicit, user-scoped, never org-scoped."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_capability(self, user_id: uuid.UUID, capability: str) -> bool:
        row = await self._session.get(PlatformGrantRecord, (user_id, capability))
        return row is not None

    async def grant(
        self, *, user_id: uuid.UUID, capability: str, granted_by: uuid.UUID | None
    ) -> None:
        record = PlatformGrantRecord(
            user_id=user_id,
            capability=capability,
            granted_by_user_id=granted_by,
            granted_at=dt.datetime.now(dt.UTC),
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # An already-granted capability is idempotent by contract.
            await self._session.rollback()
            raise UserConflictError(
                "That platform capability is already granted.", cause=exc
            ) from exc


class OrganizationSettingsRepository:
    """P05-owned settings for the bound Organization (tenant RLS)."""

    def __init__(self, tenant: TenantSession) -> None:
        self._session = tenant.session

    async def get(self) -> OrganizationSettingsRecord | None:
        return (
            (await self._session.execute(select(OrganizationSettingsRecord)))
            .scalars()
            .one_or_none()
        )

    async def insert(
        self, *, settings_id: uuid.UUID, organization_id: uuid.UUID, locale: str
    ) -> OrganizationSettingsRecord:
        record = OrganizationSettingsRecord(
            id=settings_id,
            organization_id=organization_id,
            locale=locale,
            revision=1,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return record


class DashboardConfigurationRepository:
    """Provisioned dashboard schema snapshots for the bound Organization (tenant RLS)."""

    def __init__(self, tenant: TenantSession) -> None:
        self._session = tenant.session

    async def latest(self) -> DashboardConfigurationRecord | None:
        return (
            (
                await self._session.execute(
                    select(DashboardConfigurationRecord)
                    .order_by(DashboardConfigurationRecord.revision.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def insert(
        self,
        *,
        config_id: uuid.UUID,
        organization_id: uuid.UUID,
        schema_version: int,
        revision: int,
        configuration: dict[str, Any],
    ) -> DashboardConfigurationRecord:
        record = DashboardConfigurationRecord(
            id=config_id,
            organization_id=organization_id,
            schema_version=schema_version,
            revision=revision,
            configuration=configuration,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ProvisioningStateConflictError(
                "A dashboard configuration with that revision already exists.", cause=exc
            ) from exc
        await self._session.refresh(record)
        return record
