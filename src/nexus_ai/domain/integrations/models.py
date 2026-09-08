"""Integration Hub persistence models (NXS-INT-001).

Every table is TENANT-OWNED with forced RLS (:class:`TenantOwnedMixin` +
``apply_tenant_rls`` in the migration). Rows that reference another Hub table use
COMPOSITE TENANT-AWARE foreign keys — ``(organization_id, <parent_id>)`` references
``(organization_id, id)`` on the parent — so the database itself refuses a cross-tenant
attachment (ADR-0052 pattern). No table stores a plaintext secret:
``integration_secrets`` holds Fernet ciphertext only.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class IntegrationRecord(TenantOwnedMixin, Base):
    __tablename__ = "integrations"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_integrations_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_integrations_org_slug"),
        CheckConstraint(
            "integration_type IN ('REST','OPENAPI','GRAPHQL','CRM','ERP','CALENDAR','WEBHOOK')",
            name="integration_type_known",
        ),
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','DISABLED','ERROR')", name="integration_status_known"
        ),
        CheckConstraint("config_revision >= 1", name="config_revision_positive"),
        Index("ix_integrations_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    integration_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auth_profile: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    destination_rule: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
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


class IntegrationOperationRecord(TenantOwnedMixin, Base):
    __tablename__ = "integration_operations"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id",
            "integration_id",
            "operation_key",
            name="uq_integration_operations_key",
        ),
        CheckConstraint(
            "operation_type IN ('REST','GRAPHQL')", name="integration_operation_type_known"
        ),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_integration_operations_org_integration",
            ondelete="CASCADE",
        ),
        Index(
            "ix_integration_operations_integration",
            "organization_id",
            "integration_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    spec: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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


class IntegrationSecretRecord(TenantOwnedMixin, Base):
    """Encrypted-at-rest credential storage. ``ciphertext`` is Fernet output — the
    plaintext secret is never written to PostgreSQL."""

    __tablename__ = "integration_secrets"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "credential_ref", name="uq_integration_secrets_ref"),
        CheckConstraint(
            "credential_type IN "
            "('API_KEY','BEARER_TOKEN','BASIC_AUTH','OAUTH2_CLIENT','HMAC_SECRET')",
            name="integration_secret_type_known",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    credential_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
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


class IntegrationExecutionRecord(TenantOwnedMixin, Base):
    """A bounded, safe-metadata-only trail of every execution attempt outcome."""

    __tablename__ = "integration_execution_records"
    __table_args__ = (  # type: ignore[assignment]
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_integration_execution_records_org_integration",
            ondelete="CASCADE",
        ),
        Index(
            "ix_integration_execution_records_integration",
            "organization_id",
            "integration_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    result_class: Mapped[str] = mapped_column(String(32), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    upstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class IntegrationIdempotencyRecord(TenantOwnedMixin, Base):
    __tablename__ = "integration_idempotency_records"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id",
            "integration_id",
            "operation_key",
            "idempotency_key",
            name="uq_integration_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETED','FAILED')",
            name="integration_idempotency_status_known",
        ),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_integration_idempotency_records_org_integration",
            ondelete="CASCADE",
        ),
        Index("ix_integration_idempotency_expiry", "organization_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookEndpointRecord(TenantOwnedMixin, Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_webhook_endpoints_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_webhook_endpoints_org_slug"),
        UniqueConstraint(
            "organization_id", "public_token", name="uq_webhook_endpoints_public_token"
        ),
        CheckConstraint(
            "signature_scheme IN ('NONE','HMAC_SHA256')", name="webhook_signature_scheme_known"
        ),
        CheckConstraint("tolerance_seconds BETWEEN 30 AND 3600", name="webhook_tolerance_bounds"),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_webhook_endpoints_org_integration",
            ondelete="CASCADE",
        ),
        Index("ix_webhook_endpoints_integration", "organization_id", "integration_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The unguessable public identifier used in the inbound webhook URL. It embeds the
    #: (b64) organization id so the receive path can bind the tenant scope BEFORE the
    #: RLS-scoped lookup — the payload is never trusted for tenant mapping.
    public_token: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_scheme: Mapped[str] = mapped_column(String(16), nullable=False)
    signature_header: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timestamp_header: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tolerance_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    max_body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=1_048_576)
    credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
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


class WebhookReceiptRecord(TenantOwnedMixin, Base):
    """Durable inbound-webhook dedup + processing status."""

    __tablename__ = "webhook_receipts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id",
            "webhook_endpoint_id",
            "external_id",
            name="uq_webhook_receipts_dedup",
        ),
        CheckConstraint(
            "status IN ('RECEIVED','ACCEPTED','REJECTED')", name="webhook_receipt_status_known"
        ),
        ForeignKeyConstraint(
            ["organization_id", "webhook_endpoint_id"],
            ["webhook_endpoints.organization_id", "webhook_endpoints.id"],
            name="fk_webhook_receipts_org_endpoint",
            ondelete="CASCADE",
        ),
        Index(
            "ix_webhook_receipts_endpoint",
            "organization_id",
            "webhook_endpoint_id",
            "received_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    webhook_endpoint_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
