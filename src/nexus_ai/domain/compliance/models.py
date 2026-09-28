"""Tenant-safe compliance rows; immutable decisions and durable execution identity."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


class ComplianceControl(TenantOwnedMixin, Base):
    __tablename__ = "compliance_controls"
    __table_args__: Any = (
        UniqueConstraint("organization_id"),
        CheckConstraint("policy_epoch >= 0 AND hold_epoch >= 0", name="epochs_valid"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hold_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CompliancePolicy(TenantOwnedMixin, Base):
    __tablename__ = "compliance_policies"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "revision", name="uq_compliance_policy_revision"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("state IN ('DRAFT','ACTIVE','RETIRED')", name="state_known"),
        CheckConstraint("octet_length(rules::text) <= 8192", name="rules_bounded"),
        Index(
            "uq_compliance_policy_active",
            "organization_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    activated_by: Mapped[uuid.UUID | None]
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ComplianceHold(TenantOwnedMixin, Base):
    __tablename__ = "compliance_holds"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "subject_id"], ["customers.organization_id", "customers.id"]
        ),
        CheckConstraint("state IN ('ACTIVE','RELEASED')", name="state_known"),
        Index("ix_compliance_holds_subject", "organization_id", "subject_id", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    subject_id: Mapped[uuid.UUID]
    resource_class: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(96))
    created_by: Mapped[uuid.UUID]
    released_by: Mapped[uuid.UUID | None]
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ComplianceRequest(TenantOwnedMixin, Base):
    __tablename__ = "compliance_requests"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_compliance_request_key"),
        ForeignKeyConstraint(
            ["organization_id", "subject_id"], ["customers.organization_id", "customers.id"]
        ),
        CheckConstraint("kind IN ('ACCESS','ERASURE','RESTRICTION')", name="kind_known"),
        CheckConstraint(
            "state IN ('RECEIVED','VERIFIED','PLANNED','APPROVED','EXECUTING',"
            "'COMPLETED','PARTIAL','DENIED','CANCELLED','EXPIRED')",
            name="state_known",
        ),
        CheckConstraint(
            "state <> 'COMPLETED' OR jsonb_array_length(incomplete_resources) = 0",
            name="completion_truthful",
        ),
        CheckConstraint("octet_length(result::text) <= 16384", name="result_bounded"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    subject_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(96))
    semantic_digest: Mapped[str] = mapped_column(String(64))
    verified_by: Mapped[uuid.UUID | None]
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    incomplete_resources: Mapped[list[str]] = mapped_column(JSONB, default=list)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CompliancePlan(TenantOwnedMixin, Base):
    __tablename__ = "compliance_plans"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint(
            "organization_id", "operation_identity", name="uq_compliance_plan_operation"
        ),
        ForeignKeyConstraint(
            ["organization_id", "request_id"],
            ["compliance_requests.organization_id", "compliance_requests.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "policy_id"],
            ["compliance_policies.organization_id", "compliance_policies.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "subject_id"], ["customers.organization_id", "customers.id"]
        ),
        CheckConstraint(
            "target_version > 0 AND policy_epoch > 0 AND hold_epoch >= 0", name="versions_valid"
        ),
        CheckConstraint("action IN ('ACCESS','ANONYMIZE','RESTRICT')", name="action_known"),
        CheckConstraint("resource_class = 'CUSTOMER_PROFILE'", name="resource_supported"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    request_id: Mapped[uuid.UUID | None]
    policy_id: Mapped[uuid.UUID]
    policy_revision: Mapped[int] = mapped_column(Integer)
    policy_epoch: Mapped[int] = mapped_column(Integer)
    hold_epoch: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[uuid.UUID]
    target_version: Mapped[int] = mapped_column(Integer)
    resource_class: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    operation_identity: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ComplianceApproval(TenantOwnedMixin, Base):
    __tablename__ = "compliance_approvals"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "plan_id"),
        ForeignKeyConstraint(
            ["organization_id", "plan_id"],
            ["compliance_plans.organization_id", "compliance_plans.id"],
        ),
        CheckConstraint("expires_at > issued_at", name="expiry_valid"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[uuid.UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[uuid.UUID]
    reason_code: Mapped[str] = mapped_column(String(96))
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ComplianceExecution(TenantOwnedMixin, Base):
    __tablename__ = "compliance_executions"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "plan_id"),
        ForeignKeyConstraint(
            ["organization_id", "plan_id"],
            ["compliance_plans.organization_id", "compliance_plans.id"],
        ),
        CheckConstraint("generation > 0", name="generation_positive"),
        CheckConstraint(
            "state IN ('CLAIMED','COMPLETED','FAILED','AMBIGUOUS')", name="state_known"
        ),
        CheckConstraint("octet_length(result::text) <= 16384", name="result_bounded"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[uuid.UUID]
    owner_id: Mapped[uuid.UUID]
    generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    lease_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
