"""Provisioning persistence models (NXS-ORG-001, NXS-DASH-001).

Classification is conscious and documented in ADR-0050:

* GLOBAL (no RLS): ``provisioning_requests`` — the durable idempotency ledger for a
  control-plane operation that by definition runs BEFORE any tenant scope exists. It
  stores only hashes of idempotency keys, request fingerprints and identifiers — no
  onboarding payload dumps, no secrets. Reads are always exact-key lookups.
* GLOBAL (no RLS): ``platform_grants`` — explicit platform capabilities granted to a
  user (``organization:create``). Assignments in Organizations NEVER grant platform
  capabilities, and platform grants never grant anything inside an Organization.
* TENANT-OWNED (forced RLS): ``organization_settings`` — one settings row per
  Organization, created by the provisioner.
* TENANT-OWNED (forced RLS): ``dashboard_configurations`` — the provisioned baseline
  dashboard schema snapshot, one canonical row per (organization, revision).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ProvisioningRequestRecord(Base):
    """Durable idempotency + workflow ledger for Organization provisioning.

    The row and the Organization it creates commit atomically; a PENDING row without
    an Organization is the crash-recovery state the provisioner resumes safely."""

    __tablename__ = "provisioning_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_provisioning_requests_key_hash"),
        CheckConstraint("status IN ('PENDING', 'COMPLETED', 'FAILED')", name="status_known"),
        Index("ix_provisioning_requests_organization_id", "organization_id"),
        Index("ix_provisioning_requests_organization_key", "organization_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    organization_key: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The VALIDATED canonical onboarding payload (secret-free by contract) — the
    #: crash-resume path reconstructs the request from it. Never caller-verbatim.
    request_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

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
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformGrantRecord(Base):
    """Explicit platform capability grant (user-scoped, NOT organization-scoped)."""

    __tablename__ = "platform_grants"
    __table_args__ = (
        CheckConstraint("capability IN ('organization:create')", name="capability_known"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class OrganizationSettingsRecord(TenantOwnedMixin, Base):
    """P05-owned Organization settings. One row per Organization, provisioning-created."""

    __tablename__ = "organization_settings"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint("organization_id", name="uq_organization_settings_organization"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("length(locale) >= 2", name="locale_bounded"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    locale: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

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


class DashboardConfigurationRecord(TenantOwnedMixin, Base):
    """The provisioned baseline dashboard schema snapshot.

    One canonical row per (organization, revision); the latest revision is current.
    The stored ``configuration`` is regenerated deterministically and MUST revalidate
    against the widget registry before it is ever served."""

    __tablename__ = "dashboard_configurations"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint("organization_id", "revision", name="uq_dashboard_config_org_revision"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_dashboard_config_org_revision", "organization_id", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
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
