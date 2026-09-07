"""Customer and conversation persistence models (NXS-CUSTOMER-001).

Every table is TENANT-OWNED (forced RLS) and — where rows reference another P06
table — uses COMPOSITE TENANT-AWARE foreign keys: ``(organization_id, <parent_id>)``
references ``(organization_id, id)`` on the parent, so the database itself refuses a
cross-tenant attachment (ADR-0052). RLS remains the runtime enforcement; the
composite FKs are the second, schema-level defense.

Unique constraints are the identity invariants:

* ``UNIQUE(organization_id, identity_type, normalized_value)`` — one canonical
  identity belongs to at most one Customer per Organization.
* ``UNIQUE(organization_id, channel, provider_namespace, external_thread_id)`` —
  the two external fields are nullable, and PostgreSQL treats NULLs as distinct, so
  this is one Conversation per deterministic external thread key exactly when the
  key is present (the same effect as a partial unique index, stated directly).
* ``UNIQUE(organization_id, dedup_key)`` — same NULL semantics: replay-safe timeline
  appends exactly when a dedup key is present.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class CustomerRecord(TenantOwnedMixin, Base):
    """The Customer aggregate root. Minimal PII by design: display identity plus
    explicitly-validated locale preferences — no CRM constructs."""

    __tablename__ = "customers"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint("organization_id", "id", name="uq_customers_org_id"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="status_known"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_customers_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    preferred_locale: Mapped[str | None] = mapped_column(String(32), nullable=True)
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


class CustomerIdentityRecord(TenantOwnedMixin, Base):
    """Channel identity plane. The (org, type, normalized_value) unique constraint IS
    the identity invariant and the resolution index."""

    __tablename__ = "customer_identities"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint(
            "organization_id",
            "identity_type",
            "normalized_value",
            name="uq_customer_identities_canonical",
        ),
        CheckConstraint(
            "identity_type IN ('EMAIL', 'PHONE', 'EXTERNAL_ID')", name="identity_type_known"
        ),
        CheckConstraint(
            "verification_state IN ('UNVERIFIED', 'VERIFIED', 'REVOKED')",
            name="verification_state_known",
        ),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="status_known"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_customer_identities_org_customer_customers",
            ondelete="RESTRICT",
        ),
        Index("ix_customer_identities_customer", "organization_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    identity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    verification_state: Mapped[str] = mapped_column(String(16), nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

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


class ConversationRecord(TenantOwnedMixin, Base):
    """Channel-neutral Conversation aggregate."""

    __tablename__ = "conversations"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint("organization_id", "id", name="uq_conversations_org_id"),
        UniqueConstraint(
            "organization_id",
            "channel",
            "provider_namespace",
            "external_thread_id",
            name="uq_conversations_external_thread",
        ),
        CheckConstraint("channel != ''", name="channel_nonempty"),
        CheckConstraint("status IN ('PENDING', 'OPEN', 'CLOSED')", name="status_known"),
        CheckConstraint("version >= 1", name="version_positive"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_conversations_org_customer_customers",
            ondelete="RESTRICT",
        ),
        Index("ix_conversations_customer", "organization_id", "customer_id"),
        Index("ix_conversations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_namespace: Mapped[str | None] = mapped_column(String(48), nullable=True)
    external_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConversationParticipantRecord(TenantOwnedMixin, Base):
    """Typed, validated participant references. P06 creates only CUSTOMER and SYSTEM
    participants — HUMAN_AGENT (P17) and AI_AGENT (P13) are typed seams, never fake
    records."""

    __tablename__ = "conversation_participants"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint(
            "organization_id",
            "conversation_id",
            "participant_type",
            "participant_ref",
            name="uq_conversation_participants_identity",
        ),
        CheckConstraint(
            "participant_type IN ('CUSTOMER', 'HUMAN_AGENT', 'AI_AGENT', 'SYSTEM')",
            name="participant_type_known",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_conversation_participants_org_conversation_conversations",
            ondelete="RESTRICT",
        ),
        Index("ix_conversation_participants_conversation", "organization_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    participant_type: Mapped[str] = mapped_column(String(16), nullable=False)
    participant_ref: Mapped[str] = mapped_column(String(160), nullable=False)

    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    left_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConversationActivityRecord(TenantOwnedMixin, Base):
    """The unified customer timeline primitive. ``id`` is a UUIDv7: deterministic
    (occurred_at, id) ordering without relying on timestamps alone. ``dedup_key``
    gives replay-safe appends."""

    __tablename__ = "conversation_activities"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint(
            "organization_id",
            "dedup_key",
            name="uq_conversation_activities_dedup",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_conversation_activities_org_customer_customers",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_conversation_activities_org_conversation_conversations",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_conversation_activities_timeline",
            "organization_id",
            "customer_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    activity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
