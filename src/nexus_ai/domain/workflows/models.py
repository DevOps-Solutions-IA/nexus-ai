"""PostgreSQL-authoritative workflow persistence models."""

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin

_DEFINITION_STATES = "('DRAFT','ACTIVE','ARCHIVED')"
_RUN_STATES = "('PENDING','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED')"
_STEP_STATES = "('PENDING','READY','RUNNING','COMPLETED','FAILED','SKIPPED','CANCELLED')"
_STEP_TYPES = "('TOOL','AGENT','CONDITION','NOOP')"
_ENTITY_TYPES = "('WORKFLOW_RUN','WORKFLOW_STEP_RUN')"


class WorkflowDefinitionRecord(TenantOwnedMixin, Base):
    __tablename__ = "workflow_definitions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_workflow_definitions_org_id"),
        UniqueConstraint("organization_id", "workflow_key", name="uq_workflow_definitions_org_key"),
        CheckConstraint(f"status IN {_DEFINITION_STATES}", name="ck_workflow_definition_status"),
        CheckConstraint("revision >= 1", name="ck_workflow_definition_revision"),
        Index("ix_workflow_definitions_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    workflow_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(800), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    draft_steps: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowVersionRecord(TenantOwnedMixin, Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_workflow_versions_org_id"),
        UniqueConstraint(
            "organization_id", "definition_id", "version_number", name="uq_workflow_versions_number"
        ),
        ForeignKeyConstraint(
            ["organization_id", "definition_id"],
            ["workflow_definitions.organization_id", "workflow_definitions.id"],
            name="fk_workflow_versions_org_definition",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number >= 1", name="ck_workflow_version_positive"),
        Index("ix_workflow_versions_definition", "organization_id", "definition_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    definition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowVersionStepRecord(TenantOwnedMixin, Base):
    __tablename__ = "workflow_version_steps"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_workflow_version_steps_org_id"),
        UniqueConstraint(
            "organization_id", "version_id", "step_key", name="uq_workflow_version_steps_key"
        ),
        ForeignKeyConstraint(
            ["organization_id", "version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_workflow_version_steps_org_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"step_type IN {_STEP_TYPES}", name="ck_workflow_version_step_type"),
        CheckConstraint("topological_order >= 0", name="ck_workflow_step_order"),
        Index(
            "ix_workflow_version_steps_version",
            "organization_id",
            "version_id",
            "topological_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_type: Mapped[str] = mapped_column(String(16), nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    retry_policy: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    topological_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowRunRecord(TenantOwnedMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_workflow_runs_org_id"),
        UniqueConstraint(
            "organization_id", "version_id", "idempotency_key", name="uq_workflow_runs_idempotency"
        ),
        ForeignKeyConstraint(
            ["organization_id", "definition_id"],
            ["workflow_definitions.organization_id", "workflow_definitions.id"],
            name="fk_workflow_runs_org_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_workflow_runs_org_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"state IN {_RUN_STATES}", name="ck_workflow_run_state"),
        CheckConstraint("state_version >= 1", name="ck_workflow_run_state_version"),
        Index("ix_workflow_runs_org_state", "organization_id", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    definition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    input_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    output_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowStepRunRecord(TenantOwnedMixin, Base):
    __tablename__ = "workflow_step_runs"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_workflow_step_runs_org_id"),
        UniqueConstraint("organization_id", "run_id", "step_key", name="uq_workflow_step_runs_key"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_workflow_step_runs_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "version_step_id"],
            ["workflow_version_steps.organization_id", "workflow_version_steps.id"],
            name="fk_workflow_step_runs_org_version_step",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"state IN {_STEP_STATES}", name="ck_workflow_step_run_state"),
        CheckConstraint(f"step_type IN {_STEP_TYPES}", name="ck_workflow_step_run_type"),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1", name="ck_workflow_step_attempts"
        ),
        Index(
            "ix_workflow_step_runs_ready", "organization_id", "run_id", "state", "next_eligible_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    version_step_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_type: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_eligible_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    output_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    external_execution_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowTransitionHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "workflow_transition_history"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_workflow_transition_history_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_workflow_transition_history_org_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"entity_type IN {_ENTITY_TYPES}", name="ck_workflow_transition_entity_type"
        ),
        Index("ix_workflow_transition_run", "organization_id", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(96), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
