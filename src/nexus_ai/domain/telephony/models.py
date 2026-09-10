"""Telephony Foundation persistence (NXS-P11: NXS-TEL-001).

* ``telephony_accounts``        — tenant-owned provider accounts (Asterisk / fake). A
  provider account (provider + external id) and a webhook token are GLOBALLY unique so
  no two Organizations can adopt the same trunk / routing token.
* ``telephony_phone_numbers``   — Organization-owned, provider-verified numbers. A phone
  number in E.164 form is GLOBALLY unique — it belongs to exactly one Organization
  (caller-ID governance + inbound tenancy resolution).
* ``telephony_calls``           — the normalized call record + monotonic state machine.
  Composite tenant-aware FKs to the account and the caller-ID number. A provider call id
  is unique per ``(organization_id, account_id)``.
* ``telephony_call_events``     — the durable per-provider-event idempotency + audit log
  (unique per ``(organization_id, account_id, provider_event_id)``).
* ``telephony_media_sessions``  — the media-session foundation NXS-P12 attaches an
  external voice stream to. No audio, no recording.
* ``telephony_secrets``         — Fernet ciphertext only (same encryption seam as the
  NXS-P07 vault; a dedicated table keeps the credential planes isolated).
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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin

_PROVIDERS = "('asterisk','fake')"
_DIRECTIONS = "('INBOUND','OUTBOUND')"
_STATES = (
    "('CREATED','RINGING','EARLY_MEDIA','ANSWERED','BRIDGED','ENDING',"
    "'COMPLETED','FAILED','CANCELLED','BUSY','NO_ANSWER')"
)
_ACCOUNT_STATUS = "('ACTIVE','DISABLED')"
_EVENT_TYPES = "('STATE','DTMF','MEDIA')"
_MEDIA_STATES = "('PENDING','ACTIVE','STOPPED')"
_MEDIA_DIRECTIONS = "('INBOUND','OUTBOUND','BIDIRECTIONAL')"


class TelephonyAccountRecord(TenantOwnedMixin, Base):
    __tablename__ = "telephony_accounts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_telephony_accounts_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_telephony_accounts_org_slug"),
        UniqueConstraint("provider", "external_account_id", name="uq_telephony_accounts_provider"),
        UniqueConstraint("webhook_token", name="uq_telephony_accounts_webhook_token"),
        CheckConstraint(f"provider IN {_PROVIDERS}", name="ck_telephony_accounts_provider_known"),
        CheckConstraint(f"status IN {_ACCOUNT_STATUS}", name="ck_telephony_accounts_status_known"),
        Index("ix_telephony_accounts_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    webhook_token: Mapped[str] = mapped_column(String(96), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TelephonyPhoneNumberRecord(TenantOwnedMixin, Base):
    __tablename__ = "telephony_phone_numbers"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_telephony_phone_numbers_org_id"),
        UniqueConstraint("e164", name="uq_telephony_phone_numbers_e164"),
        CheckConstraint(
            r"e164 ~ '^\+[1-9][0-9]{6,14}$'", name="ck_telephony_phone_numbers_e164_form"
        ),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            name="fk_telephony_phone_numbers_org_account",
            ondelete="RESTRICT",
        ),
        Index("ix_telephony_phone_numbers_organization_id", "organization_id"),
        Index(
            "ix_telephony_phone_numbers_account",
            "organization_id",
            "account_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    e164: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TelephonyCallRecord(TenantOwnedMixin, Base):
    __tablename__ = "telephony_calls"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_telephony_calls_org_id"),
        UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_call_id",
            name="uq_telephony_calls_provider_call_id",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_telephony_calls_org_idempotency_key",
        ),
        CheckConstraint(f"direction IN {_DIRECTIONS}", name="ck_telephony_calls_direction_known"),
        CheckConstraint(f"state IN {_STATES}", name="ck_telephony_calls_state_known"),
        CheckConstraint("state_rank BETWEEN 0 AND 6", name="ck_telephony_calls_state_rank_bounds"),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            name="fk_telephony_calls_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "from_number_id"],
            ["telephony_phone_numbers.organization_id", "telephony_phone_numbers.id"],
            name="fk_telephony_calls_org_from_number",
            # PostgreSQL 15+ column-specific SET NULL (ADR-0077): purging a retired number
            # NULLs only this pointer; organization_id (NOT NULL) is left unchanged.
            ondelete="SET NULL (from_number_id)",
        ),
        Index("ix_telephony_calls_organization_id", "organization_id"),
        Index("ix_telephony_calls_state", "organization_id", "state"),
        Index(
            "ix_telephony_calls_account_created",
            "organization_id",
            "account_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    from_number_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    from_address: Mapped[str] = mapped_column(String(128), nullable=False)
    to_address: Mapped[str] = mapped_column(String(128), nullable=False)
    legs: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Canonical hash of the semantic outbound-call fields — every idempotency path
    #: compares this so a re-used key with a different caller ID / destination / account
    #: / metadata is a deterministic conflict, not a silent replay.
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_timestamp: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ringing_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TelephonyCallEventRecord(TenantOwnedMixin, Base):
    __tablename__ = "telephony_call_events"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_event_id",
            name="uq_telephony_call_events_provider_event",
        ),
        CheckConstraint(
            f"event_type IN {_EVENT_TYPES}", name="ck_telephony_call_events_type_known"
        ),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            name="fk_telephony_call_events_org_account",
            ondelete="CASCADE",
        ),
        Index("ix_telephony_call_events_organization_id", "organization_id"),
        Index(
            "ix_telephony_call_events_call",
            "organization_id",
            "call_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    call_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    applied_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TelephonyMediaSessionRecord(TenantOwnedMixin, Base):
    __tablename__ = "telephony_media_sessions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_telephony_media_sessions_org_id"),
        CheckConstraint(
            f"state IN {_MEDIA_STATES}", name="ck_telephony_media_sessions_state_known"
        ),
        CheckConstraint(
            f"direction IN {_MEDIA_DIRECTIONS}",
            name="ck_telephony_media_sessions_direction_known",
        ),
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_telephony_media_sessions_org_call",
            ondelete="CASCADE",
        ),
        Index("ix_telephony_media_sessions_organization_id", "organization_id"),
        Index("ix_telephony_media_sessions_call", "organization_id", "call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    call_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    bridge_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    stream_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    codec: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TelephonySecretRecord(TenantOwnedMixin, Base):
    __tablename__ = "telephony_secrets"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "credential_ref", name="uq_telephony_secrets_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    credential_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(24), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
