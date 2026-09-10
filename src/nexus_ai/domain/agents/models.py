"""AI Agent Runtime persistence (NXS-P13: NXS-AGENT-001).

Every table is TENANT-OWNED with forced RLS. An ``ai_agent_sessions`` row references its
model profile, agent, and (optionally) its NXS-P06 customer / conversation and its
NXS-P11 call / NXS-P12 voice session with COMPOSITE TENANT-AWARE foreign keys
``(organization_id, <id>) -> (organization_id, id)`` so a session can only ever bind to
rows in its own Organization. No model API key, endpoint, raw provider payload or chain
of thought lives in any table — ``ai_model_secrets`` holds Fernet ciphertext only, and
``ai_agent_turns`` stores the bounded conversation text (user input + final answer), not
reasoning.
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin

_PROVIDERS = "('openai_compatible','fake')"
_ACCOUNT_STATUS = "('ACTIVE','DISABLED')"
_PROFILE_STATUS = "('ACTIVE','DISABLED')"
_AGENT_STATUS = "('ACTIVE','DISABLED')"
_CHANNELS = "('API','VOICE','WHATSAPP','EMAIL','SMS')"
_SESSION_STATES = (
    "('PENDING','ACTIVE','WAITING_TOOL','RESPONDING','COMPLETED','FAILED','CANCELLED','EXPIRED')"
)
_TURN_STATES = (
    "('PENDING','RUNNING','AWAITING_TOOLS','FINALIZING','COMPLETED','FAILED','CANCELLED')"
)
_TOOLCALL_STATUS = "('REQUESTED','ALLOWED','DENIED','COMPLETED','FAILED')"


class AiModelProviderAccountRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_model_provider_accounts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_model_provider_accounts_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_ai_model_provider_accounts_org_slug"),
        UniqueConstraint(
            "provider", "external_account_id", name="uq_ai_model_provider_accounts_provider"
        ),
        CheckConstraint(
            f"provider IN {_PROVIDERS}", name="ck_ai_model_provider_accounts_provider_known"
        ),
        CheckConstraint(
            f"status IN {_ACCOUNT_STATUS}", name="ck_ai_model_provider_accounts_status_known"
        ),
        Index("ix_ai_model_provider_accounts_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    api_base: Mapped[str] = mapped_column(String(256), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiModelSecretRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_model_secrets"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "credential_ref", name="uq_ai_model_secrets_ref"),
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


class AiModelProfileRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_model_profiles"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_model_profiles_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_ai_model_profiles_org_slug"),
        CheckConstraint(f"status IN {_PROFILE_STATUS}", name="ck_ai_model_profiles_status_known"),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["ai_model_provider_accounts.organization_id", "ai_model_provider_accounts.id"],
            name="fk_ai_model_profiles_org_account",
            ondelete="RESTRICT",
        ),
        Index("ix_ai_model_profiles_organization_id", "organization_id"),
        Index("ix_ai_model_profiles_account", "organization_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiAgentRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_agents"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_agents_org_id"),
        UniqueConstraint("organization_id", "slug", name="uq_ai_agents_org_slug"),
        CheckConstraint(f"status IN {_AGENT_STATUS}", name="ck_ai_agents_status_known"),
        CheckConstraint(
            "max_tool_iterations BETWEEN 0 AND 32", name="ck_ai_agents_tool_iterations_bounds"
        ),
        ForeignKeyConstraint(
            ["organization_id", "model_profile_id"],
            ["ai_model_profiles.organization_id", "ai_model_profiles.id"],
            name="fk_ai_agents_org_profile",
            ondelete="RESTRICT",
        ),
        Index("ix_ai_agents_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    model_profile_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    system_instructions: Mapped[str] = mapped_column(Text, nullable=False)
    tool_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    max_tool_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiAgentSessionRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_agent_sessions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_agent_sessions_org_id"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_ai_agent_sessions_org_idempotency_key"
        ),
        CheckConstraint(f"state IN {_SESSION_STATES}", name="ck_ai_agent_sessions_state_known"),
        CheckConstraint(f"channel IN {_CHANNELS}", name="ck_ai_agent_sessions_channel_known"),
        CheckConstraint(
            "state_rank BETWEEN 0 AND 4", name="ck_ai_agent_sessions_state_rank_bounds"
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["ai_agents.organization_id", "ai_agents.id"],
            name="fk_ai_agent_sessions_org_agent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "model_profile_id"],
            ["ai_model_profiles.organization_id", "ai_model_profiles.id"],
            name="fk_ai_agent_sessions_org_profile",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_ai_agent_sessions_org_customer",
            ondelete="SET NULL (customer_id)",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_ai_agent_sessions_org_conversation",
            ondelete="SET NULL (conversation_id)",
        ),
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_ai_agent_sessions_org_call",
            ondelete="SET NULL (call_id)",
        ),
        ForeignKeyConstraint(
            ["organization_id", "voice_session_id"],
            ["voice_sessions.organization_id", "voice_sessions.id"],
            name="fk_ai_agent_sessions_org_voice_session",
            ondelete="SET NULL (voice_session_id)",
        ),
        Index("ix_ai_agent_sessions_organization_id", "organization_id"),
        Index("ix_ai_agent_sessions_agent", "organization_id", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    model_profile_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    state_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    initiator_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    initiator_session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    voice_session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiAgentTurnRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_agent_turns"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_agent_turns_org_id"),
        UniqueConstraint("organization_id", "session_id", "sequence", name="uq_ai_agent_turns_seq"),
        CheckConstraint(f"state IN {_TURN_STATES}", name="ck_ai_agent_turns_state_known"),
        CheckConstraint(f"channel IN {_CHANNELS}", name="ck_ai_agent_turns_channel_known"),
        ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_ai_agent_turns_org_session",
            ondelete="CASCADE",
        ),
        Index("ix_ai_agent_turns_organization_id", "organization_id"),
        Index("ix_ai_agent_turns_session", "organization_id", "session_id"),
        Index(
            "uq_ai_agent_turns_idempotency",
            "organization_id",
            "session_id",
            "idempotency_key",
            unique=True,
            postgresql_where="idempotency_key IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    response_char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model: Mapped[str | None] = mapped_column(String(96), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_iterations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiAgentToolCallRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_agent_tool_calls"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_agent_tool_calls_org_id"),
        CheckConstraint(
            f"status IN {_TOOLCALL_STATUS}", name="ck_ai_agent_tool_calls_status_known"
        ),
        ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["ai_agent_turns.organization_id", "ai_agent_turns.id"],
            name="fk_ai_agent_tool_calls_org_turn",
            ondelete="CASCADE",
        ),
        Index("ix_ai_agent_tool_calls_organization_id", "organization_id"),
        Index("ix_ai_agent_tool_calls_turn", "organization_id", "turn_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_key: Mapped[str] = mapped_column(String(96), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tool_result_class: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tool_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    denied_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiModelUsageRecord(TenantOwnedMixin, Base):
    __tablename__ = "ai_model_usage"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_ai_model_usage_org_id"),
        UniqueConstraint("organization_id", "session_id", name="uq_ai_model_usage_session"),
        ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_ai_model_usage_org_session",
            ondelete="CASCADE",
        ),
        Index("ix_ai_model_usage_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
