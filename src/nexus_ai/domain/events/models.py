"""Event platform persistence models (NXS-DATA-002, NXS-EVENT-003/006/007).

Classification is conscious and documented in ADR-0047 / ADR-0048:

* TENANT-OWNED (forced RLS bound to ``nxs.organization_id``): ``event_outbox``,
  ``event_dead_letters`` — each row carries a trusted ``organization_id`` and is only
  visible inside that tenant's transaction scope, plus an unscoped relay policy so the
  system publisher and dead-letter tooling can process every tenant's rows from an
  explicitly unscoped system transaction (never from a tenant request).
* PLATFORM-INTERNAL (no RLS): ``consumer_receipts`` — idempotency bookkeeping only
  (consumer name, globally unique event id, outcome, timestamps). It holds no tenant
  business content, is only ever read by exact ``(consumer_name, event_id)`` and must
  also cover global-scoped events that have no ``organization_id``.

Only tenant-scoped events use the outbox; global platform events are published directly
through the confirmed JetStream publisher (ADR-0047).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class EventOutboxRecord(TenantOwnedMixin, Base):
    """One row per tenant business event. The row and the business mutation that
    produced it commit in the same database transaction (NXS-EVENT-003). The row moves
    to ``PUBLISHED`` only after JetStream acknowledges the publication."""

    __tablename__ = "event_outbox"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        CheckConstraint(
            "status IN ('PENDING', 'PUBLISHING', 'PUBLISHED', 'FAILED', 'DEAD')",
            name="status_known",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        Index("ix_event_outbox_claim", "status", "available_at"),
        Index("ix_event_outbox_lease", "lease_expires_at"),
    )

    # id == envelope.event_id: enqueuing the same event twice is a primary-key conflict.
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    envelope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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


class EventDeadLetterRecord(TenantOwnedMixin, Base):
    """A tenant event that failed terminally (poison payload, exhausted retries or an
    explicit terminal handler failure). Retains identity and sanitized failure
    information — never a stack trace or a secret (NXS-EVENT-007)."""

    __tablename__ = "event_dead_letters"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        CheckConstraint("origin IN ('OUTBOX_PUBLISH', 'CONSUMER')", name="origin_known"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        Index("ix_event_dead_letters_event_id", "event_id"),
        Index("ix_event_dead_letters_recorded_at", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    consumer_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_class: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    envelope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    replayed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ConsumerReceiptRecord(Base):
    """PLATFORM-INTERNAL idempotency bookkeeping (NXS-EVENT-006). No RLS: it holds no
    tenant business content, must also cover global events, and is only ever addressed
    by the exact ``(consumer_name, event_id)`` pair."""

    __tablename__ = "consumer_receipts"
    # The composite primary key (consumer_name, event_id) IS the uniqueness guarantee —
    # no redundant UNIQUE constraint (PostgreSQL 17 absorbs it into the PK index anyway).
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSING', 'PROCESSED', 'FAILED', 'DEAD')", name="status_known"
        ),
        CheckConstraint("attempt_count >= 1", name="attempt_count_positive"),
        Index("ix_consumer_receipts_event_id", "event_id"),
    )

    consumer_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PROCESSING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
