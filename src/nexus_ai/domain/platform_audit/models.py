"""Separate immutable source journal, ledger, receipt and integrity head."""

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base


class PlatformAuditIntentRecord(Base):
    __tablename__ = "platform_audit_intents"
    __table_args__ = (
        CheckConstraint("octet_length(payload::text) <= 8192", name="payload_bounded"),
        CheckConstraint(
            "(payload->>'scope' = 'PLATFORM' AND "
            "payload->'organization_id' = 'null'::jsonb) IS TRUE",
            name="platform_scope",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class PlatformAuditHead(Base):
    __tablename__ = "platform_audit_heads"
    __table_args__ = (CheckConstraint("id = 1 AND sequence >= 0", name="singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str] = mapped_column(String(64))


class PlatformAuditRecord(Base):
    __tablename__ = "platform_audit_records"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="positive_sequence"),
        CheckConstraint("octet_length(fact::text) <= 8192", name="fact_bounded"),
        CheckConstraint(
            "(fact->>'scope' = 'PLATFORM' AND fact->'organization_id' = 'null'::jsonb AND "
            "fact->'intent'->>'scope' = 'PLATFORM' AND "
            "fact->'intent'->'organization_id' = 'null'::jsonb) IS TRUE",
            name="platform_scope",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("platform_audit_intents.id"), unique=True)
    sequence: Mapped[int] = mapped_column(BigInteger, unique=True)
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    semantic_digest: Mapped[str] = mapped_column(String(64))
    predecessor: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    fact: Mapped[dict[str, Any]] = mapped_column(JSONB)


class PlatformAuditReceipt(Base):
    __tablename__ = "platform_audit_receipts"
    __table_args__ = (
        CheckConstraint(
            "((record_id IS NOT NULL AND reason_code IS NULL) OR "
            "(record_id IS NULL AND reason_code = 'INVALID_INTENT')) IS TRUE",
            name="result_valid",
        ),
    )
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_audit_intents.id"), primary_key=True
    )
    record_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_audit_records.id"), unique=True
    )
    reason_code: Mapped[str | None] = mapped_column(String(32))
