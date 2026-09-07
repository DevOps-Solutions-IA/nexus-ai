"""Typed tenant-scoped unit of work (NXS-TENANT-004).

A ``TenantSession`` carries the ``organization_id`` alongside the underlying
``AsyncSession``. Tenant repositories and services require this wrapper, so an
unscoped ``AsyncSession`` cannot accidentally be used on a tenant code path — the
security boundary lives in infrastructure, not in every repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.core.tenancy import tenant_namespace


@dataclass(frozen=True, slots=True)
class TenantSession:
    organization_id: UUID
    session: AsyncSession

    def namespace(self, *segments: str) -> str:
        return tenant_namespace(self.organization_id, *segments)
