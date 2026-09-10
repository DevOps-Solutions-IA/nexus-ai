"""ElevenLabs Voice persistence (NXS-P12: NXS-VOICE-001 / NXS-EL-001).

* ``voice_provider_accounts`` — tenant-owned provider accounts (elevenlabs / fake). A
  provider account (provider + external id) and a webhook token are GLOBALLY unique.
* ``voice_secrets``           — Fernet ciphertext only (same seam as the NXS-P07 vault).
* ``voice_profiles``          — Organization-owned voice configuration resources. A
  caller references one of these by id; a raw provider ``voice_id`` / ``agent_id`` is
  never accepted from the API.
* ``voice_sessions``          — the real-time session record + monotonic state machine.
  COMPOSITE TENANT-AWARE foreign keys into ``telephony_calls`` and
  ``telephony_media_sessions`` (NXS-P11 stays authoritative for the call). One live
  session per media session (partial unique index). No audio, no transcript body.
* ``voice_provider_events``   — the durable per-provider-event idempotency + audit log
  (unique per ``(organization_id, account_id, provider_event_id)``).
* ``voice_usage_records``     — bounded numeric usage / latency per session. No audio.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin

_PROVIDERS = "('elevenlabs','fake')"
_ACCOUNT_STATUS = "('ACTIVE','DISABLED')"
_PROFILE_STATUS = "('ACTIVE','DISABLED')"
_DIRECTIONS = "('INBOUND','OUTBOUND')"
_STATES = (
    "('PENDING','CONNECTING','CONNECTED','STREAMING','ENDING','COMPLETED','FAILED','CANCELLED')"
)
_HANDOFF = "('AI','PENDING_HUMAN','HUMAN')"
_EVENT_KINDS = "('SESSION','TRANSCRIPT','MEDIA','USAGE','ERROR')"
_LIVE_STATES = "'PENDING','CONNECTING','CONNECTED','STREAMING','ENDING'"


class VoiceProviderAccountRecord(TenantOwnedMixin, Base):
    __tablename__ = "voice_provider_accounts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_voice_provider_accounts_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_voice_provider_accounts_org_slug"),
        UniqueConstraint(
            "provider", "external_account_id", name="uq_voice_provider_accounts_provider"
        ),
        UniqueConstraint("webhook_token", name="uq_voice_provider_accounts_webhook_token"),
        CheckConstraint(
            f"provider IN {_PROVIDERS}", name="ck_voice_provider_accounts_provider_known"
        ),
        CheckConstraint(
            f"status IN {_ACCOUNT_STATUS}", name="ck_voice_provider_accounts_status_known"
        ),
        Index("ix_voice_provider_accounts_organization_id", "organization_id"),
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


class VoiceSecretRecord(TenantOwnedMixin, Base):
    __tablename__ = "voice_secrets"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "credential_ref", name="uq_voice_secrets_ref"),
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


class VoiceProfileRecord(TenantOwnedMixin, Base):
    __tablename__ = "voice_profiles"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_voice_profiles_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_voice_profiles_org_slug"),
        CheckConstraint(f"status IN {_PROFILE_STATUS}", name="ck_voice_profiles_status_known"),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["voice_provider_accounts.organization_id", "voice_provider_accounts.id"],
            name="fk_voice_profiles_org_account",
            ondelete="RESTRICT",
        ),
        Index("ix_voice_profiles_organization_id", "organization_id"),
        Index("ix_voice_profiles_account", "organization_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_voice_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_model_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_format: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    output_format: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VoiceSessionRecord(TenantOwnedMixin, Base):
    __tablename__ = "voice_sessions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_voice_sessions_org_id"),
        UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_session_id",
            name="uq_voice_sessions_provider_session_id",
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_voice_sessions_org_idempotency_key"
        ),
        CheckConstraint(f"direction IN {_DIRECTIONS}", name="ck_voice_sessions_direction_known"),
        CheckConstraint(f"state IN {_STATES}", name="ck_voice_sessions_state_known"),
        CheckConstraint(f"handoff_state IN {_HANDOFF}", name="ck_voice_sessions_handoff_known"),
        CheckConstraint("state_rank BETWEEN 0 AND 5", name="ck_voice_sessions_state_rank_bounds"),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["voice_provider_accounts.organization_id", "voice_provider_accounts.id"],
            name="fk_voice_sessions_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "voice_profile_id"],
            ["voice_profiles.organization_id", "voice_profiles.id"],
            name="fk_voice_sessions_org_profile",
            ondelete="SET NULL (voice_profile_id)",
        ),
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_voice_sessions_org_call",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "media_session_id"],
            ["telephony_media_sessions.organization_id", "telephony_media_sessions.id"],
            name="fk_voice_sessions_org_media_session",
            ondelete="RESTRICT",
        ),
        Index("ix_voice_sessions_organization_id", "organization_id"),
        Index("ix_voice_sessions_call", "organization_id", "call_id"),
        Index(
            "uq_voice_sessions_one_live_per_media",
            "organization_id",
            "media_session_id",
            unique=True,
            postgresql_where=text(f"state IN ({_LIVE_STATES})"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    voice_profile_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    call_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    media_session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    handoff_state: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bridge_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    negotiated_format: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    usage: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_timestamp: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connecting_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    connected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VoiceProviderEventRecord(TenantOwnedMixin, Base):
    __tablename__ = "voice_provider_events"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_event_id",
            name="uq_voice_provider_events_provider_event",
        ),
        CheckConstraint(f"kind IN {_EVENT_KINDS}", name="ck_voice_provider_events_kind_known"),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["voice_provider_accounts.organization_id", "voice_provider_accounts.id"],
            name="fk_voice_provider_events_org_account",
            ondelete="CASCADE",
        ),
        Index("ix_voice_provider_events_organization_id", "organization_id"),
        Index("ix_voice_provider_events_session", "organization_id", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VoiceUsageRecordRecord(TenantOwnedMixin, Base):
    __tablename__ = "voice_usage_records"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_voice_usage_records_org_id"),
        UniqueConstraint("organization_id", "session_id", name="uq_voice_usage_records_session"),
        ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["voice_sessions.organization_id", "voice_sessions.id"],
            name="fk_voice_usage_records_org_session",
            ondelete="CASCADE",
        ),
        Index("ix_voice_usage_records_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    audio_seconds_in: Mapped[float] = mapped_column(Float, nullable=False)
    audio_seconds_out: Mapped[float] = mapped_column(Float, nullable=False)
    provider_characters: Mapped[int] = mapped_column(Integer, nullable=False)
    interruptions: Mapped[int] = mapped_column(Integer, nullable=False)
    time_to_first_audio_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
