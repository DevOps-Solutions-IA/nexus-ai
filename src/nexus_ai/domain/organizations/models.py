"""Organization persistence model (NXS-ORG-002).

The ``organizations`` table is self-scoped: forced RLS binds each row to the
transaction-local ``nxs.organization_id`` on the primary key. No business concerns
(billing, quotas, users, channels, dashboards, providers) live here — those are later
phases.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import TENANT_SCOPED_KEY, TENANT_SELF, Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class OrganizationRecord(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("organization_key", name="uq_organizations_organization_key"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "status IN ('PROVISIONING', 'ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="status_known",
        ),
        Index("ix_organizations_status", "status"),
        {"info": {TENANT_SCOPED_KEY: TENANT_SELF}},
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    organization_key: Mapped[str] = mapped_column(String(48), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    industry_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tax_identifier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
