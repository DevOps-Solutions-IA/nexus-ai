"""Minimal DID discovery projection with exact P11 source binding."""

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
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import TENANT_OWNED, TENANT_SCOPED_KEY, Base, TenantOwnedMixin


class SipDidLocatorRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_did_locators"
    __table_args__: Any = (
        UniqueConstraint("e164"),
        UniqueConstraint("phone_number_id"),
        UniqueConstraint("organization_id", "id", name="uq_sip_did_locators_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "phone_number_id", "account_id", "e164"],
            [
                "telephony_phone_numbers.organization_id",
                "telephony_phone_numbers.id",
                "telephony_phone_numbers.account_id",
                "telephony_phone_numbers.e164",
            ],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(r"e164 ~ '^\+[1-9][0-9]{6,14}$'", name="e164_form"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    phone_number_id: Mapped[UUID] = mapped_column()
    account_id: Mapped[UUID] = mapped_column(index=True)
    e164: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CellSipTargetRecord(Base):
    __tablename__ = "cell_sip_targets"
    __table_args__ = (
        UniqueConstraint("cell_id", "target_revision", name="uq_cell_sip_target_revision"),
        UniqueConstraint("cell_id", "id", name="uq_cell_sip_target_identity"),
        CheckConstraint("target_revision > 0", name="revision_positive"),
        CheckConstraint("port BETWEEN 1024 AND 65535", name="port_bounded"),
        CheckConstraint("transport IN ('UDP','TCP','TLS')", name="transport_known"),
        CheckConstraint(
            "state IN ('REGISTERED','ACTIVE','DRAINING','RETIRED')", name="state_known"
        ),
        Index(
            "uq_cell_sip_target_active",
            "cell_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"))
    target_revision: Mapped[int] = mapped_column(BigInteger)
    host: Mapped[str] = mapped_column(String(45))
    port: Mapped[int] = mapped_column()
    transport: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CellSipTargetHeadRecord(Base):
    __tablename__ = "cell_sip_target_heads"
    __table_args__ = (
        CheckConstraint("control_revision >= 0", name="revision_nonnegative"),
        ForeignKeyConstraint(
            ["cell_id", "active_target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
    )
    cell_id: Mapped[UUID] = mapped_column(
        ForeignKey("cells.id", ondelete="RESTRICT"), primary_key=True
    )
    control_revision: Mapped[int] = mapped_column(BigInteger)
    active_target_id: Mapped[UUID | None] = mapped_column()


class SipTargetMutationRecord(Base):
    __tablename__ = "sip_target_mutations"
    __table_args__ = (
        UniqueConstraint("operation", "key_hash"),
        CheckConstraint(
            "operation IN ('REGISTER','ACTIVE','DRAINING','RETIRED')", name="operation_known"
        ),
        CheckConstraint("result_revision > expected_revision", name="revision_advanced"),
        ForeignKeyConstraint(
            ["cell_id", "target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_id: Mapped[UUID] = mapped_column()
    target_id: Mapped[UUID] = mapped_column()
    operation: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    expected_revision: Mapped[int] = mapped_column(BigInteger)
    result_revision: Mapped[int] = mapped_column(BigInteger)
    result_state: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason_code: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[UUID] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
