"""Messaging channel persistence models (NXS-P09).

* ``messaging_accounts``       — tenant-owned channel/provider accounts. A provider
  account (channel + provider + external id) and a webhook token are GLOBALLY unique so
  no two Organizations can adopt the same phone number / mailbox / routing token.
* ``messaging_messages``       — the normalized message record. Composite tenant-aware
  FKs to ``conversations``, ``customers`` and ``messaging_accounts``. Inbound provider
  message ids are unique per (org, account, direction) for deduplication.
* ``messaging_inbound_receipts`` — the durable per-event dedupe / replay claim.
* ``messaging_secrets``        — Fernet ciphertext only (same encryption seam as the
  NXS-P07 vault; a dedicated table keeps the two credential planes isolated).
* ``messaging_send_idempotency`` — durable outbound-send idempotency claims.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
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


_CHANNELS = "('WHATSAPP','EMAIL','SMS')"
_DIRECTIONS = "('INBOUND','OUTBOUND')"
_STATUSES = "('RECEIVED','QUEUED','SENDING','SENT','DELIVERED','READ','FAILED')"


class MessagingAccountRecord(TenantOwnedMixin, Base):
    __tablename__ = "messaging_accounts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_messaging_accounts_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_messaging_accounts_org_slug"),
        UniqueConstraint(
            "channel", "provider", "external_account_id", name="uq_messaging_accounts_provider"
        ),
        UniqueConstraint("webhook_token", name="uq_messaging_accounts_webhook_token"),
        CheckConstraint(f"channel IN {_CHANNELS}", name="channel_known"),
        CheckConstraint("status IN ('ACTIVE','DISABLED')", name="status_known"),
        Index("ix_messaging_accounts_channel", "organization_id", "channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_identity: Mapped[str] = mapped_column(String(320), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    webhook_token: Mapped[str] = mapped_column(String(96), nullable=False)

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


class MessagingMessageRecord(TenantOwnedMixin, Base):
    __tablename__ = "messaging_messages"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_messaging_messages_org_id"),
        UniqueConstraint(
            "organization_id",
            "account_id",
            "direction",
            "provider_message_id",
            name="uq_messaging_messages_provider_id",
        ),
        CheckConstraint(f"channel IN {_CHANNELS}", name="channel_known"),
        CheckConstraint(f"direction IN {_DIRECTIONS}", name="direction_known"),
        CheckConstraint(f"status IN {_STATUSES}", name="status_known"),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_messaging_messages_org_conversation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_messaging_messages_org_customer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_messaging_messages_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "reply_to_message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_messaging_messages_org_reply_parent",
            # PostgreSQL 15+ column-specific SET NULL: deleting a parent message NULLs
            # ONLY reply_to_message_id on its children — organization_id (NOT NULL) is
            # left unchanged, so tenant isolation is preserved and the delete succeeds.
            ondelete="SET NULL (reply_to_message_id)",
        ),
        Index(
            "ix_messaging_messages_conversation",
            "organization_id",
            "conversation_id",
            "created_at",
        ),
        Index("ix_messaging_messages_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sender: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    recipients: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    email: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    sms: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_timestamp: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class MessagingInboundReceiptRecord(TenantOwnedMixin, Base):
    __tablename__ = "messaging_inbound_receipts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id", "account_id", "event_id", name="uq_messaging_inbound_receipts_event"
        ),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_messaging_inbound_receipts_org_account",
            ondelete="CASCADE",
        ),
        Index(
            "ix_messaging_inbound_receipts_account",
            "organization_id",
            "account_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    event_id: Mapped[str] = mapped_column(String(400), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class MessagingSecretRecord(TenantOwnedMixin, Base):
    __tablename__ = "messaging_secrets"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "credential_ref", name="uq_messaging_secrets_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    credential_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(24), nullable=False)
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


class MessagingSendIdempotencyRecord(TenantOwnedMixin, Base):
    __tablename__ = "messaging_send_idempotency"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id",
            "account_id",
            "idempotency_key",
            name="uq_messaging_send_idempotency_key",
        ),
        CheckConstraint("status IN ('PENDING','COMPLETED','FAILED')", name="status_known"),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_messaging_send_idempotency_org_account",
            ondelete="CASCADE",
        ),
        Index(
            "ix_messaging_send_idempotency_expiry",
            "organization_id",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
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
