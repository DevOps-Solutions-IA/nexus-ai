"""Bounded platform records owned exclusively by the Sentinel database identity."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base


class SentinelIncident(Base):
    __tablename__ = "sentinel_incidents"
    __table_args__ = (
        Index(
            "uq_sentinel_incidents_active_correlation",
            "correlation_key",
            unique=True,
            postgresql_where=text("state NOT IN ('RESOLVED','CLOSED')"),
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "state IN ('OPEN','TRIAGED','MITIGATION_PROPOSED','MITIGATING',"
            "'MONITORING','RESOLVED','CLOSED')",
            name="state_known",
        ),
        CheckConstraint(
            "subject_kind IN ('GLOBAL','SERVICE','CELL','ORGANIZATION')", name="subject_known"
        ),
        CheckConstraint("severity IN ('INFO','WARNING','ERROR','CRITICAL')", name="severity_known"),
        CheckConstraint(
            "resolution_source IS NULL OR resolution_source IN "
            "('TRUSTED_RECOVERY','AUTHORIZED_OPERATOR')",
            name="resolution_known",
        ),
        CheckConstraint(
            "state NOT IN ('RESOLVED','CLOSED') OR resolution_source IS NOT NULL",
            name="resolution_required",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    correlation_key: Mapped[str] = mapped_column(String(64))
    subject_kind: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[UUID]
    organization_id: Mapped[UUID | None]
    state: Mapped[str] = mapped_column(String(24), index=True)
    severity: Mapped[str] = mapped_column(String(8))
    revision: Mapped[int] = mapped_column(BigInteger)
    summary: Mapped[str] = mapped_column(String(256))
    resolution_source: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SentinelSignalReceipt(Base):
    __tablename__ = "sentinel_signal_receipts"
    __table_args__ = (
        UniqueConstraint("adapter_id", "source_identity", "source_observation_id"),
        CheckConstraint("adapter_revision > 0", name="revision_positive"),
        CheckConstraint("schema_version = 1", name="schema_known"),
        CheckConstraint("status IN ('RECORDED','CORRELATED')", name="status_known"),
        CheckConstraint("octet_length(payload::text) <= 32768", name="payload_bounded"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String(64))
    adapter_revision: Mapped[int] = mapped_column(Integer)
    source_identity: Mapped[str] = mapped_column(String(64))
    source_observation_id: Mapped[str] = mapped_column(String(64))
    semantic_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    incident_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sentinel_incidents.id", ondelete="RESTRICT"), index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SentinelFinding(Base):
    __tablename__ = "sentinel_findings"
    __table_args__ = (
        UniqueConstraint("incident_id", "revision"),
        CheckConstraint("revision > 0 AND revision <= 100", name="revision_bounded"),
        CheckConstraint("octet_length(payload::text) <= 16384", name="payload_bounded"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("sentinel_incidents.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SentinelRunbook(Base):
    __tablename__ = "sentinel_runbooks"
    __table_args__ = (
        UniqueConstraint("key", "revision"),
        UniqueConstraint("id", "revision"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("key ~ '^[a-zA-Z0-9_.:-]{1,64}$'", name="key_safe"),
        CheckConstraint("handler_key ~ '^[a-zA-Z0-9_.:-]{1,64}$'", name="handler_safe"),
        CheckConstraint(
            "risk IN ('OBSERVE','DIAGNOSTIC','REVERSIBLE','HIGH_IMPACT','DESTRUCTIVE')",
            name="risk_known",
        ),
        CheckConstraint("octet_length(definition::text) <= 8192", name="definition_bounded"),
        CheckConstraint("jsonb_typeof(definition) = 'object'", name="definition_object"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    handler_key: Mapped[str] = mapped_column(String(64))
    risk: Mapped[str] = mapped_column(String(16))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    semantic_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SentinelActionProposal(Base):
    __tablename__ = "sentinel_action_proposals"
    __table_args__ = (
        UniqueConstraint("fingerprint"),
        UniqueConstraint("id", "fingerprint", "policy_revision"),
        ForeignKeyConstraint(
            ["runbook_id", "runbook_revision"],
            ["sentinel_runbooks.id", "sentinel_runbooks.revision"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('PROPOSED','APPROVED','REJECTED','EXPIRED','EXECUTING',"
            "'SUCCEEDED','FAILED','AMBIGUOUS','CANCELLED')",
            name="state_known",
        ),
        CheckConstraint("incident_revision > 0 AND policy_revision > 0", name="revisions_positive"),
        CheckConstraint("octet_length(payload::text) <= 8192", name="payload_bounded"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint("expires_at > created_at", name="expiry_valid"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("sentinel_incidents.id", ondelete="RESTRICT"), index=True
    )
    incident_revision: Mapped[int] = mapped_column(BigInteger)
    runbook_id: Mapped[UUID]
    runbook_revision: Mapped[int] = mapped_column(Integer)
    policy_revision: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SentinelApproval(Base):
    __tablename__ = "sentinel_approvals"
    __table_args__ = (
        UniqueConstraint("proposal_id", "approver_principal"),
        ForeignKeyConstraint(
            ["proposal_id", "proposal_fingerprint", "policy_revision"],
            [
                "sentinel_action_proposals.id",
                "sentinel_action_proposals.fingerprint",
                "sentinel_action_proposals.policy_revision",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("decision IN ('APPROVED','REJECTED')", name="decision_known"),
        CheckConstraint("policy_revision > 0", name="revision_positive"),
        CheckConstraint("expires_at > issued_at", name="expiry_valid"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(index=True)
    proposal_fingerprint: Mapped[str] = mapped_column(String(64))
    approver_principal: Mapped[UUID]
    decision: Mapped[str] = mapped_column(String(8))
    reason_code: Mapped[str] = mapped_column(String(64))
    policy_revision: Mapped[int] = mapped_column(BigInteger)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SentinelExecution(Base):
    __tablename__ = "sentinel_executions"
    __table_args__ = (
        UniqueConstraint("proposal_id"),
        UniqueConstraint("idempotency_key"),
        CheckConstraint("execution_generation > 0", name="generation_positive"),
        CheckConstraint(
            "dispatch_state IN ('CLAIMED','DISPATCHED','COMPLETED','AMBIGUOUS')", name="state_known"
        ),
        CheckConstraint("lease_expires_at > started_at", name="lease_valid"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completion_valid"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("sentinel_action_proposals.id", ondelete="RESTRICT")
    )
    execution_generation: Mapped[int] = mapped_column(BigInteger)
    owner_id: Mapped[UUID]
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    dispatch_state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_classification: Mapped[str | None] = mapped_column(String(64))
    error_classification: Mapped[str | None] = mapped_column(String(64))
    external_reference: Mapped[UUID | None]


class SentinelControlState(Base):
    __tablename__ = "sentinel_control_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    revision: Mapped[int] = mapped_column(BigInteger)
    mutable_actions_enabled: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
