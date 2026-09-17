"""PostgreSQL-authoritative Campaign persistence models."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
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


class CampaignRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaigns_org_id"),
        UniqueConstraint("organization_id", "campaign_key", name="uq_campaigns_org_key"),
        CheckConstraint(
            "state IN ('DRAFT','PREPARING','READY','SCHEDULED','RUNNING','PAUSED',"
            "'CANCELLING','COMPLETED','FAILED','CANCELLED')",
            name="ck_campaigns_state",
        ),
        CheckConstraint("revision >= 1", name="ck_campaigns_revision"),
        Index("ix_campaigns_state", "organization_id", "state", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    campaign_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    draft_specification: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    prepared_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CampaignRevisionRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_revisions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_revisions_org_id"),
        UniqueConstraint(
            "organization_id", "campaign_id", "revision_number", name="uq_campaign_revision_number"
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_revisions_org_campaign",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "release_workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_campaign_revisions_org_release_workflow",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "recipient_workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_campaign_revisions_org_recipient_workflow",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_campaign_revisions_org_account",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_campaign_revisions_campaign", "organization_id", "campaign_id", "revision_number"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    release_workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    recipient_workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    specification: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignAudienceSnapshotRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_audience_snapshots"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_audience_snapshots_org_id"),
        UniqueConstraint(
            "organization_id", "campaign_revision_id", name="uq_campaign_audience_revision"
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_revision_id"],
            ["campaign_revisions.organization_id", "campaign_revisions.id"],
            name="fk_campaign_audience_org_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("state IN ('OPEN','SEALED','FAILED')", name="ck_campaign_audience_state"),
        CheckConstraint(
            "cursor >= 0 AND resolved_count >= 0 AND eligible_count >= 0 AND rejected_count >= 0",
            name="ck_campaign_audience_counts",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    campaign_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(200))
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    resolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sealed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignRecipientRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_recipients_org_id"),
        UniqueConstraint(
            "organization_id", "snapshot_id", "customer_id", "channel", name="uq_campaign_recipient"
        ),
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["campaign_audience_snapshots.organization_id", "campaign_audience_snapshots.id"],
            name="fk_campaign_recipients_org_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_campaign_recipients_org_customer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "identity_id"],
            ["customer_identities.organization_id", "customer_identities.id"],
            name="fk_campaign_recipients_org_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_campaign_recipients_org_conversation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('PENDING','ELIGIBLE','DEFERRED','SUPPRESSED','CANCELLED')",
            name="ck_campaign_recipients_state",
        ),
        Index(
            "ix_campaign_recipients_work",
            "organization_id",
            "snapshot_id",
            "state",
            "next_eligible_at",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    identity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(48), nullable=False)
    next_eligible_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    evaluated_consent_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    evaluated_suppression_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignRunRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_runs"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_runs_org_id"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_campaign_runs_idempotency"),
        UniqueConstraint("organization_id", "schedule_id", name="uq_campaign_runs_schedule"),
        UniqueConstraint(
            "organization_id",
            "release_schedule_occurrence_id",
            name="uq_campaign_runs_release_occurrence",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_runs_org_campaign",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_revision_id"],
            ["campaign_revisions.organization_id", "campaign_revisions.id"],
            name="fk_campaign_runs_org_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "audience_snapshot_id"],
            ["campaign_audience_snapshots.organization_id", "campaign_audience_snapshots.id"],
            name="fk_campaign_runs_org_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "release_workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_campaign_runs_org_release_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "release_schedule_occurrence_id"],
            ["scheduler_occurrences.organization_id", "scheduler_occurrences.id"],
            name="fk_campaign_runs_org_release_occurrence",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["scheduler_schedules.organization_id", "scheduler_schedules.id"],
            name="fk_campaign_runs_org_schedule",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('PENDING_RELEASE','MATERIALIZING','RUNNING','PAUSED','CANCELLING',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="ck_campaign_runs_state",
        ),
        CheckConstraint(
            "authorized_count >= 0 AND attempt_materialization_cursor >= 0 "
            "AND cancellation_processed_count >= 0",
            name="ck_campaign_runs_progress",
        ),
        CheckConstraint(
            "release_schedule_occurrence_id IS NULL OR "
            "(schedule_id IS NOT NULL AND release_workflow_run_id IS NOT NULL)",
            name="ck_campaign_runs_release_binding",
        ),
        Index("ix_campaign_runs_state", "organization_id", "state", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    campaign_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    audience_snapshot_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    release_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    release_schedule_occurrence_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    claimed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dispatched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    suppressed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    authorized_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    attempt_materialization_cursor: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    attempt_materialization_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    cancellation_processed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    cancellation_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CampaignRecipientAttemptRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_recipient_attempts"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_attempts_org_id"),
        UniqueConstraint(
            "organization_id", "logical_identity", name="uq_campaign_attempt_identity"
        ),
        UniqueConstraint(
            "organization_id", "p14_idempotency_key", name="uq_campaign_attempt_p14_key"
        ),
        UniqueConstraint(
            "organization_id", "p09_idempotency_key", name="uq_campaign_attempt_p09_key"
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_run_id"],
            ["campaign_runs.organization_id", "campaign_runs.id"],
            name="fk_campaign_attempts_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "recipient_id"],
            ["campaign_recipients.organization_id", "campaign_recipients.id"],
            name="fk_campaign_attempts_org_recipient",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_campaign_attempts_org_workflow_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_campaign_attempts_org_message",
            ondelete="RESTRICT",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_campaign_attempt_number"),
        CheckConstraint(
            "state IN ('PENDING','CLAIMED','WORKFLOW_RUNNING','READY_TO_SEND',"
            "'DISPATCH_AUTHORIZED','DISPATCHED','SUPPRESSED','FAILED','CANCELLED')",
            name="ck_campaign_attempt_state",
        ),
        Index(
            "ix_campaign_attempts_claim",
            "organization_id",
            "campaign_run_id",
            "state",
            "created_at",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    campaign_run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    recipient_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    logical_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    p14_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    p09_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CampaignSendPermitRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_send_permits"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_permits_org_id"),
        UniqueConstraint(
            "organization_id", "recipient_attempt_id", name="uq_campaign_permit_attempt"
        ),
        UniqueConstraint(
            "organization_id", "p09_idempotency_key", name="uq_campaign_permit_p09_key"
        ),
        ForeignKeyConstraint(
            ["organization_id", "recipient_attempt_id"],
            ["campaign_recipient_attempts.organization_id", "campaign_recipient_attempts.id"],
            name="fk_campaign_permits_org_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_campaign_permits_org_message",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('PENDING','AUTHORIZED','CONSUMED','FAILED','EXPIRED')",
            name="ck_campaign_permit_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    recipient_attempt_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    permit_token: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, unique=True
    )
    consent_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    suppression_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    p09_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    authorized_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))


class CampaignContactPreferenceRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_contact_preferences"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_preferences_org_id"),
        UniqueConstraint(
            "organization_id", "identity_id", "channel", name="uq_campaign_preference_identity"
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_campaign_preferences_org_customer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "identity_id"],
            ["customer_identities.organization_id", "customer_identities.id"],
            name="fk_campaign_preferences_org_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint("consent_epoch >= 1", name="ck_campaign_preference_epoch"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    identity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    consent_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    unsubscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    consent_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    evidence_ref: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignPolicyEpochRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_policy_epochs"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_policy_epochs_org_id"),
        UniqueConstraint("organization_id", "channel", name="uq_campaign_policy_epoch_channel"),
        CheckConstraint("suppression_epoch >= 1", name="ck_campaign_policy_epoch"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    suppression_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignSuppressionRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_suppressions"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_suppressions_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_suppressions_org_campaign",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_campaign_suppressions_org_customer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "identity_id"],
            ["customer_identities.organization_id", "customer_identities.id"],
            name="fk_campaign_suppressions_org_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "scope IN ('GLOBAL','CAMPAIGN','CUSTOMER','IDENTITY')",
            name="ck_campaign_suppression_scope",
        ),
        Index("ix_campaign_suppressions_lookup", "organization_id", "channel", "active", "scope"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    identity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignThrottleWindowRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_throttle_windows"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_throttle_windows_org_id"),
        UniqueConstraint(
            "organization_id",
            "campaign_run_id",
            "channel",
            "window_start",
            name="uq_campaign_throttle_window",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_run_id"],
            ["campaign_runs.organization_id", "campaign_runs.id"],
            name="fk_campaign_throttle_org_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint("reserved_count >= 0", name="ck_campaign_throttle_count"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    campaign_run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reserved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class CampaignOrganizationThrottleWindowRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_organization_throttle_windows"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_org_throttle_windows_org_id"),
        UniqueConstraint(
            "organization_id",
            "channel",
            "window_start",
            name="uq_campaign_org_throttle_window",
        ),
        CheckConstraint("reserved_count >= 0", name="ck_campaign_org_throttle_count"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reserved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class CampaignTransitionHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "campaign_transition_history"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("organization_id", "id", name="uq_campaign_history_org_id"),
        CheckConstraint(
            "entity_type IN ('CAMPAIGN','RUN','RECIPIENT','ATTEMPT','PERMIT')",
            name="ck_campaign_history_entity",
        ),
        Index(
            "ix_campaign_history_entity",
            "organization_id",
            "entity_type",
            "entity_id",
            "created_at",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(96), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
