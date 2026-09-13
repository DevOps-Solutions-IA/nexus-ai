"""PostgreSQL-authoritative Scheduler persistence models."""

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


class SchedulerScheduleRecord(TenantOwnedMixin, Base):
    __tablename__ = "scheduler_schedules"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_scheduler_schedules_org_id"),
        UniqueConstraint("organization_id", "schedule_key", name="uq_scheduler_schedules_org_key"),
        ForeignKeyConstraint(
            ["organization_id", "workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_scheduler_schedules_org_workflow_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("target_type = 'START_WORKFLOW'", name="ck_scheduler_schedule_target"),
        CheckConstraint(
            "schedule_type IN ('ONE_TIME','RECURRING')", name="ck_scheduler_schedule_type"
        ),
        CheckConstraint(
            "state IN ('DRAFT','ACTIVE','PAUSED','COMPLETED','CANCELLED')",
            name="ck_scheduler_schedule_state",
        ),
        CheckConstraint(
            "misfire_policy IN ('SKIP','FIRE_ONCE','CATCH_UP_BOUNDED')",
            name="ck_scheduler_schedule_misfire",
        ),
        CheckConstraint("max_catch_up BETWEEN 1 AND 100", name="ck_scheduler_schedule_catch_up"),
        CheckConstraint("revision >= 1", name="ck_scheduler_schedule_revision"),
        CheckConstraint(
            "end_at IS NULL OR end_at >= start_at", name="ck_scheduler_schedule_window"
        ),
        Index("ix_scheduler_schedules_due", "organization_id", "state", "next_fire_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    schedule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    recurrence_spec: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    input_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_local_time: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    max_catch_up: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    timezone_data_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SchedulerOccurrenceRecord(TenantOwnedMixin, Base):
    __tablename__ = "scheduler_occurrences"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_scheduler_occurrences_org_id"),
        UniqueConstraint(
            "organization_id",
            "schedule_id",
            "occurrence_key",
            name="uq_scheduler_occurrences_identity",
        ),
        UniqueConstraint(
            "organization_id", "p14_idempotency_key", name="uq_scheduler_occurrences_p14_key"
        ),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["scheduler_schedules.organization_id", "scheduler_schedules.id"],
            name="fk_scheduler_occurrences_org_schedule",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_scheduler_occurrences_org_workflow_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_scheduler_occurrences_org_workflow_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('PENDING','CLAIMED','DISPATCHED','FAILED','SKIPPED','CANCELLED')",
            name="ck_scheduler_occurrence_state",
        ),
        CheckConstraint("fold IN (0,1)", name="ck_scheduler_occurrence_fold"),
        CheckConstraint(
            "schedule_revision >= 1 AND dispatch_attempt_count >= 0",
            name="ck_scheduler_occurrence_attempts",
        ),
        Index("ix_scheduler_occurrences_due", "organization_id", "state", "scheduled_for"),
        Index("ix_scheduler_occurrences_schedule", "organization_id", "schedule_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    schedule_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    schedule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    scheduled_for: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    intended_local_time: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=False), nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    utc_offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    fold: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    timezone_data_version: Mapped[str] = mapped_column(String(32), nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_owner_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatch_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    p14_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    workflow_input: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SchedulerTransitionHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "scheduler_transition_history"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_scheduler_transition_history_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["scheduler_schedules.organization_id", "scheduler_schedules.id"],
            name="fk_scheduler_transition_history_org_schedule",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "occurrence_id"],
            ["scheduler_occurrences.organization_id", "scheduler_occurrences.id"],
            name="fk_scheduler_transition_history_org_occurrence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "entity_type IN ('SCHEDULE','OCCURRENCE')", name="ck_scheduler_transition_entity"
        ),
        Index(
            "ix_scheduler_transition_entity",
            "organization_id",
            "entity_type",
            "entity_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    schedule_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
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
