"""Global catalog and tenant-owned placement authority and immutable receipts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import TENANT_OWNED, TENANT_SCOPED_KEY, Base, TenantOwnedMixin


class CellRecord(Base):
    __tablename__ = "cells"
    __table_args__ = (
        UniqueConstraint("cell_key"),
        UniqueConstraint("registration_key_hash"),
        CheckConstraint("state IN ('REGISTERED','RETIRED')", name="state_known"),
        CheckConstraint("cell_key ~ '^[a-z][a-z0-9-]{0,47}$'", name="key_safe"),
        CheckConstraint("NOT (state = 'RETIRED' AND has_placements)", name="retirement_unbound"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_key: Mapped[str] = mapped_column(String(48))
    registration_key_hash: Mapped[str] = mapped_column(String(64))
    registration_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    has_placements: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CellControlHistoryRecord(Base):
    __tablename__ = "cell_control_history"
    __table_args__ = (
        UniqueConstraint("operation", "idempotency_key_hash"),
        CheckConstraint("operation IN ('REGISTER','RETIRE')", name="operation_known"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"), index=True)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(String(16))
    idempotency_key_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[UUID] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OrganizationPlacementRecord(TenantOwnedMixin, Base):
    __tablename__ = "organization_placements"
    __table_args__: Any = (
        UniqueConstraint("organization_id"),
        UniqueConstraint("organization_id", "id", name="uq_organization_placements_org_id"),
        CheckConstraint("assignment_generation >= 1", name="generation_positive"),
        CheckConstraint("state IN ('ACTIVE','SUSPENDED')", name="state_known"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"), index=True)
    assignment_generation: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlacementMutationRecord(TenantOwnedMixin, Base):
    __tablename__ = "placement_mutations"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "operation", "idempotency_key_hash"),
        ForeignKeyConstraint(
            ["organization_id", "placement_id"],
            ["organization_placements.organization_id", "organization_placements.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("operation IN ('ASSIGN','SUSPEND','RESUME')", name="operation_known"),
        CheckConstraint("result_generation >= 1", name="result_generation_positive"),
        CheckConstraint(
            "expected_generation IS NULL OR expected_generation >= 1",
            name="expected_generation_positive",
        ),
        CheckConstraint("new_state IN ('ACTIVE','SUSPENDED')", name="new_state_known"),
        CheckConstraint(
            "previous_state IS NULL OR previous_state IN ('ACTIVE','SUSPENDED')",
            name="previous_state_known",
        ),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    placement_id: Mapped[UUID] = mapped_column(index=True)
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(String(16))
    idempotency_key_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    expected_generation: Mapped[int | None] = mapped_column(BigInteger)
    result_generation: Mapped[int] = mapped_column(BigInteger)
    previous_state: Mapped[str | None] = mapped_column(String(16))
    new_state: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason_code: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[UUID] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
