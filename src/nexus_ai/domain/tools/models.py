"""Tool Engine persistence models (NXS-TOOL-001).

Every table is TENANT-OWNED with forced RLS. A ``tool_definitions`` row references the
integration it is bound to with a COMPOSITE TENANT-AWARE foreign key
``(organization_id, integration_id) -> (organization_id, id)`` on ``integrations``, so a
tool can only ever bind an integration in its own Organization (ADR-0052 pattern). Stored
schemas are bounded JSONB — never executable code, never evaluated.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
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


class ToolDefinitionRecord(TenantOwnedMixin, Base):
    __tablename__ = "tool_definitions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_tool_definitions_org_id"),
        UniqueConstraint("organization_id", "tool_key", name="uq_tool_definitions_org_key"),
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','DISABLED','ERROR')", name="tool_status_known"
        ),
        CheckConstraint(
            "risk_class IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="tool_risk_class_known"
        ),
        CheckConstraint(
            "side_effect_class IN "
            "('READ_ONLY','IDEMPOTENT_WRITE','NON_IDEMPOTENT_WRITE','EXTERNAL_EFFECT')",
            name="tool_side_effect_class_known",
        ),
        CheckConstraint(
            "idempotency_policy IN ('NONE','OPTIONAL','REQUIRED')",
            name="tool_idempotency_policy_known",
        ),
        CheckConstraint("binding_type IN ('INTEGRATION')", name="tool_binding_type_known"),
        CheckConstraint("version >= 1", name="tool_version_positive"),
        CheckConstraint(
            "binding_type <> 'INTEGRATION' OR "
            "(integration_id IS NOT NULL AND operation_key IS NOT NULL)",
            name="tool_integration_binding_complete",
        ),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_tool_definitions_org_integration",
            ondelete="RESTRICT",
        ),
        Index("ix_tool_definitions_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tool_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(800), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_class: Mapped[str] = mapped_column(String(16), nullable=False)
    side_effect_class: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    required_permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    binding_type: Mapped[str] = mapped_column(String(16), nullable=False)
    integration_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    static_arguments: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
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


class ToolExecutionRecord(TenantOwnedMixin, Base):
    """Bounded, safe-metadata-only trail of every tool invocation outcome."""

    __tablename__ = "tool_execution_records"
    __table_args__ = (  # type: ignore[assignment]
        CheckConstraint("retry_count >= 0", name="tool_execution_retry_nonneg"),
        ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tool_definitions.organization_id", "tool_definitions.id"],
            name="fk_tool_execution_records_org_tool",
            ondelete="CASCADE",
        ),
        Index(
            "ix_tool_execution_records_tool",
            "organization_id",
            "tool_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tool_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_class: Mapped[str] = mapped_column(String(32), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downstream_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    caller_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ToolIdempotencyRecord(TenantOwnedMixin, Base):
    __tablename__ = "tool_idempotency_records"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id", "tool_id", "idempotency_key", name="uq_tool_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETED','FAILED')", name="tool_idempotency_status_known"
        ),
        ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tool_definitions.organization_id", "tool_definitions.id"],
            name="fk_tool_idempotency_records_org_tool",
            ondelete="CASCADE",
        ),
        Index("ix_tool_idempotency_expiry", "organization_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tool_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(64), nullable=False)
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
