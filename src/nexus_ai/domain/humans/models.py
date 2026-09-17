"""PostgreSQL-authoritative P17 persistence models."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


class HumanQueueRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_queues"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_queues_org_id"),
        UniqueConstraint("organization_id", "queue_key", name="uq_human_queues_org_key"),
        CheckConstraint("max_active_assignments BETWEEN 1 AND 100", name="capacity_bounds"),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    queue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    supported_channels: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    required_skills: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    max_active_assignments: Mapped[int] = mapped_column(Integer, nullable=False)
    sla_target_seconds: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HumanAgentPresenceRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_agent_presence"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_presence_org_id"),
        UniqueConstraint("organization_id", "agent_user_id", name="uq_human_presence_org_agent"),
        ForeignKeyConstraint(
            ["organization_id", "agent_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_human_presence_org_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('OFFLINE','AVAILABLE','BUSY','AWAY','WRAP_UP')", name="state_known"
        ),
        CheckConstraint("capacity BETWEEN 0 AND 100", name="capacity_bounds"),
        CheckConstraint(
            "active_assignment_count BETWEEN 0 AND capacity", name="active_count_bounds"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    agent_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    active_assignment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HumanWorkItemRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_work_items"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_work_items_org_id"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_human_work_items_idempotency"
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_human_work_org_conversation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_human_work_org_customer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "queue_id"],
            ["human_queues.organization_id", "human_queues.id"],
            name="fk_human_work_org_queue",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_human_work_org_call",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "voice_session_id"],
            ["voice_sessions.organization_id", "voice_sessions.id"],
            name="fk_human_work_org_voice_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assigned_agent_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_human_work_org_assigned_agent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "current_assignment_id"],
            ["human_assignments.organization_id", "human_assignments.id"],
            name="fk_human_work_org_current_assignment",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "state IN ('QUEUED','CLAIMED','ACCEPTED','ACTIVE','AI_RETURN_PENDING',"
            "'WRAP_UP','COMPLETED','CANCELLED')",
            name="state_known",
        ),
        CheckConstraint("priority BETWEEN -1000 AND 1000", name="priority_bounds"),
        CheckConstraint("lease_version >= 0", name="lease_version_nonnegative"),
        Index(
            "ix_human_work_routing",
            "organization_id",
            "queue_id",
            "state",
            text("priority DESC"),
            "eligible_at",
            "created_at",
            "id",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    call_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    voice_session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    queue_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    handoff_reason: Mapped[str] = mapped_column(String(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    eligible_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    current_assignment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    queued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_claim_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConversationOwnershipRecord(TenantOwnedMixin, Base):
    __tablename__ = "conversation_ownership"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_conversation_ownership_org_id"),
        UniqueConstraint(
            "organization_id", "conversation_id", name="uq_conversation_ownership_org_conversation"
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_conversation_ownership_org_conversation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_conversation_ownership_org_work",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assignment_id"],
            ["human_assignments.organization_id", "human_assignments.id"],
            name="fk_conversation_ownership_org_assignment",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_conversation_ownership_org_agent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "ai_session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_conversation_ownership_org_ai_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint("mode IN ('AI','HUMAN','UNASSIGNED')", name="mode_known"),
        CheckConstraint("ownership_generation >= 1", name="generation_positive"),
        CheckConstraint(
            "(mode = 'AI' AND ai_session_id IS NOT NULL AND assignment_id IS NULL "
            "AND agent_user_id IS NULL) OR "
            "(mode = 'HUMAN' AND assignment_id IS NOT NULL AND agent_user_id IS NOT NULL "
            "AND ai_session_id IS NULL) OR "
            "(mode = 'UNASSIGNED' AND assignment_id IS NULL AND agent_user_id IS NULL "
            "AND ai_session_id IS NULL)",
            name="authority_shape",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    ownership_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    agent_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    ai_session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HumanAssignmentRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_assignments"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_assignments_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_human_assignments_org_work",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "queue_id"],
            ["human_queues.organization_id", "human_queues.id"],
            name="fk_human_assignments_org_queue",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_agent_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_human_assignments_org_agent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('CLAIMED','ACCEPTED','ACTIVE','RELEASED','TRANSFERRED','COMPLETED')",
            name="state_known",
        ),
        CheckConstraint("lease_version >= 1", name="lease_positive"),
        Index(
            "uq_human_assignments_active_work",
            "organization_id",
            "work_item_id",
            unique=True,
            postgresql_where=text("state IN ('CLAIMED','ACCEPTED','ACTIVE')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    work_item_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    queue_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    owner_agent_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HumanHandoffRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_handoffs"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_handoffs_org_id"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_human_handoffs_idempotency"
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_human_handoffs_org_conversation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_human_handoffs_org_work",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "p13_session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_human_handoffs_org_p13_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint("direction IN ('AI_TO_HUMAN','HUMAN_TO_AI')", name="direction_known"),
        CheckConstraint(
            "state IN ('REQUESTED','AI_RETURN_PENDING','ACCEPTED','P13_REJECTED',"
            "'AMBIGUOUS','COMPLETED')",
            name="state_known",
        ),
        CheckConstraint(
            "(direction = 'AI_TO_HUMAN' AND p13_idempotency_key IS NULL "
            "AND p13_contract_version IS NULL) OR "
            "(direction = 'HUMAN_TO_AI' AND p13_idempotency_key IS NOT NULL "
            "AND p13_contract_version IS NOT NULL)",
            name="p13_contract_shape",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    work_item_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ownership_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    p13_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    p13_contract_version: Mapped[str | None] = mapped_column(String(64))
    p13_session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HumanActionAuthorizationRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_action_authorizations"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_action_auth_org_id"),
        UniqueConstraint(
            "organization_id", "p09_idempotency_key", name="uq_human_action_auth_p09_key"
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_human_action_auth_org_conversation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_human_action_auth_org_work",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assignment_id"],
            ["human_assignments.organization_id", "human_assignments.id"],
            name="fk_human_action_auth_org_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_human_action_auth_org_message",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('AUTHORIZED','CONSUMED','FAILED','AMBIGUOUS')", name="state_known"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    work_item_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    assignment_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ownership_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    p09_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HumanTransitionHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "human_transition_history"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_human_history_org_id"),
        Index(
            "ix_human_history_entity", "organization_id", "entity_type", "entity_id", "created_at"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(24))
    new_state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(96), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
