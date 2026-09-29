"""Per-Organization immutable facts and independently locked chain heads."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


class AuditHead(TenantOwnedMixin, Base):
    __tablename__ = "audit_heads"
    __table_args__: Any = (
        UniqueConstraint("organization_id"),
        CheckConstraint("id = organization_id", name="tenant_identity_valid"),
        CheckConstraint("sequence >= 0", name="sequence_valid"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest_valid"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str] = mapped_column(String(64))


class AuditRecord(TenantOwnedMixin, Base):
    __tablename__ = "audit_records"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "sequence", name="uq_audit_records_sequence"),
        UniqueConstraint(
            "organization_id", "producer", "source_id", name="uq_audit_records_source"
        ),
        ForeignKeyConstraint(
            ["organization_id", "original_record_id"],
            ["audit_records.organization_id", "audit_records.id"],
        ),
        CheckConstraint("sequence > 0", name="sequence_valid"),
        CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$' AND predecessor ~ '^[0-9a-f]{64}$' "
            "AND semantic_digest ~ '^[0-9a-f]{64}$'",
            name="digests_valid",
        ),
        CheckConstraint("octet_length(fact::text) <= 8192", name="fact_bounded"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    producer: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[uuid.UUID]
    original_record_id: Mapped[uuid.UUID | None]
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    semantic_digest: Mapped[str] = mapped_column(String(64))
    predecessor: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    fact: Mapped[dict[str, Any]] = mapped_column(JSONB)
