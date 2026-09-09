"""OTP challenge persistence (NXS-P10: NXS-OTP-001).

``otp_challenges`` is TENANT-OWNED with forced RLS. It references its delivery messaging
account and (once delivered) its message with COMPOSITE TENANT-AWARE foreign keys
``(organization_id, <id>) -> (organization_id, id)`` so a challenge can only ever bind
to rows in its own Organization. The plaintext OTP is never stored: ``code_hash`` is an
HMAC keyed by the configured pepper over the canonical challenge context and the code.

A PARTIAL UNIQUE INDEX on ``(organization_id, destination_fingerprint, purpose)
WHERE status = 'ACTIVE'`` enforces the "one live code per subject/purpose" policy at the
database — a concurrent double-issue resolves to exactly one active challenge.
"""

from __future__ import annotations

import datetime as dt
import uuid

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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin

_STATUSES = "('ACTIVE','VERIFIED','EXPIRED','REVOKED','LOCKED')"
_CHANNELS = "('SMS','EMAIL')"
_SUBJECT_TYPES = "('DESTINATION')"


class OtpChallengeRecord(TenantOwnedMixin, Base):
    __tablename__ = "otp_challenges"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_otp_challenges_org_id"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_otp_challenges_org_idempotency_key"
        ),
        CheckConstraint(f"status IN {_STATUSES}", name="ck_otp_challenges_status_known"),
        CheckConstraint(f"channel IN {_CHANNELS}", name="ck_otp_challenges_channel_known"),
        CheckConstraint(
            f"subject_type IN {_SUBJECT_TYPES}", name="ck_otp_challenges_subject_type_known"
        ),
        CheckConstraint("attempts >= 0", name="ck_otp_challenges_attempts_non_negative"),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10", name="ck_otp_challenges_max_attempts_bounds"
        ),
        CheckConstraint("attempts <= max_attempts", name="ck_otp_challenges_attempts_within_limit"),
        CheckConstraint("expires_at > issued_at", name="ck_otp_challenges_expiry_after_issue"),
        ForeignKeyConstraint(
            ["organization_id", "messaging_account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_otp_challenges_org_messaging_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "delivery_message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_otp_challenges_org_delivery_message",
            # PostgreSQL 15+ column-specific SET NULL (ADR-0077): a purge of the delivery
            # message NULLs only this pointer; organization_id (NOT NULL) is untouched.
            ondelete="SET NULL (delivery_message_id)",
        ),
        Index(
            "ix_otp_challenges_subject_window",
            "organization_id",
            "destination_fingerprint",
            "purpose",
            "issued_at",
        ),
        Index("ix_otp_challenges_expiry", "organization_id", "expires_at"),
        Index(
            "uq_otp_challenges_one_active",
            "organization_id",
            "destination_fingerprint",
            "purpose",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    destination: Mapped[str] = mapped_column(String(320), nullable=False)
    destination_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(48), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    messaging_account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivery_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resend_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
