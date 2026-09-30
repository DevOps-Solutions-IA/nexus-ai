"""Finite source contracts and versioned canonical integrity encoding."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai.events.errors import HandlerTerminalError
from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload

GENESIS = "0" * 64
SERVICES = frozenset(
    {
        "organization-bootstrap",
        "organization-lifecycle",
        "membership-bootstrap",
        "auth-session-guard",
        "customer-channel",
        "workflow-runtime",
        "campaign-scheduler",
        "human-operations",
        "agent-runtime",
        "tool-runtime",
        "compliance",
        "organization-service",
        "customer-service",
        "workflow-service",
        "campaign-service",
        "human-service",
        "agent-service",
    }
)
ACTIONS: dict[str, frozenset[str]] = {
    "organization": frozenset(
        {
            "organization.created",
            "organization.profile_updated",
            "organization.transitioned",
            "organization.provisioned",
        }
    ),
    "auth": frozenset(
        {
            "auth.membership.created",
            "auth.membership.suspended",
            "auth.membership.revoked",
            "auth.membership.restored",
            "auth.role.assigned",
            "auth.role.suspended",
            "auth.session.created",
            "auth.session.refreshed",
            "auth.session.revoked",
        }
    ),
    "customer": frozenset(
        {
            "customer.created",
            "customers.created",
            "customers.updated",
            "customers.identity.linked",
            "customers.identity.status_changed",
            "conversations.opened",
            "conversations.closed",
        }
    ),
    "tool": frozenset(
        {"tool.execution.dispatch_authorized", "tool.execution.completed", "tool.execution.failed"}
    ),
    "agent": frozenset(
        {
            "agent.account.created",
            "agent.account.updated",
            "agent.account.credential_stored",
            "agent.profile.created",
            "agent.profile.updated",
            "agent.definition.created",
            "agent.definition.updated",
            "agent.session.created",
            "agent.session.started",
            "agent.session.completed",
            "agent.session.failed",
            "agent.session.cancelled",
            "agent.session.expired",
            "agent.turn.started",
            "agent.turn.completed",
            "agent.turn.failed",
            "agent.tool.requested",
            "agent.tool.completed",
            "agent.tool.failed",
            "agent.response.ready",
            "agent.usage.recorded",
        }
    ),
    "workflow": frozenset(
        {
            "workflow.definition.created",
            "workflow.definition.updated",
            "workflow.version.published",
            "workflow.run.started",
            "workflow.run.paused",
            "workflow.run.resumed",
            "workflow.run.completed",
            "workflow.run.failed",
            "workflow.run.cancelled",
            "workflow.step.ready",
            "workflow.step.started",
            "workflow.step.completed",
            "workflow.step.failed",
            "workflow.step.skipped",
        }
    ),
    "campaign": frozenset(
        {
            "campaign.updated",
            "campaign.contact_preference.changed",
            "campaign.suppression.added",
            "campaign.created",
            "campaign.prepared",
            "campaign.scheduled",
            "campaign.started",
            "campaign.paused",
            "campaign.resumed",
            "campaign.cancelled",
            "campaign.completed",
            "campaign.failed",
            "campaign.recipient.eligible",
            "campaign.recipient.suppressed",
            "campaign.recipient.claimed",
            "campaign.recipient.workflow_started",
            "campaign.recipient.dispatched",
            "campaign.recipient.failed",
        }
    ),
    "human": frozenset(
        {
            "human.queue.created",
            "human.queue.updated",
            "human.message.authorized",
            "human.message.finalized",
            "human.handoff.ambiguous",
            "human.work.queued",
            "human.work.claimed",
            "human.work.accepted",
            "human.work.transferred",
            "human.work.requeued",
            "human.work.completed",
            "human.work.cancelled",
            "human.handoff.ai_to_human",
            "human.handoff.human_to_ai",
            "human.presence.changed",
            "human.supervisor.released",
        }
    ),
    "compliance": frozenset({"compliance.state.changed"}),
}
TARGETS = frozenset(
    {
        "organization",
        "membership",
        "role_assignment",
        "session",
        "customer",
        "identity",
        "customer_identity",
        "conversation",
        "tool_execution",
        "agent",
        "agent_session",
        "agent_turn",
        "agent_tool_call",
        "workflow_definition",
        "workflow_version",
        "workflow_run",
        "workflow_step",
        "workflow_step_run",
        "campaign",
        "campaign_recipient",
        "human_work",
        "human_assignment",
        "human_handoff",
        "human_presence",
        "human_queue",
        "compliance_policy",
        "compliance_hold",
        "compliance_request",
        "compliance_plan",
        "compliance_execution",
        "audit_record",
        "model_provider_account",
        "model_profile",
        "human_operations",
        "human_authorization",
        "compliance_subject_request",
        "compliance_approval",
    }
)


class AuditConflict(HandlerTerminalError):
    code = "NXS_AUDIT_SEMANTIC_CONFLICT"
    status = 409
    title = "Audit source identity conflict"


class AuditProvenanceError(HandlerTerminalError):
    code = "NXS_AUDIT_PROVENANCE_INVALID"
    title = "Audit source provenance invalid"


class AuditActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["HUMAN", "AI_AGENT", "SYSTEM", "SERVICE"]
    user_id: UUID | None = None
    agent_id: UUID | None = None
    session_id: UUID | None = None
    turn_id: UUID | None = None
    initiating_user_id: UUID | None = None
    service: str | None = Field(default=None, max_length=48)

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if self.kind == "HUMAN":
            valid = self.user_id is not None and not any(
                (
                    self.agent_id,
                    self.turn_id,
                    self.initiating_user_id,
                    self.service,
                )
            )
        elif self.kind == "AI_AGENT":
            valid = bool(self.agent_id and self.session_id) and not (self.user_id or self.service)
        else:
            valid = self.service in SERVICES and not any(
                (
                    self.user_id,
                    self.agent_id,
                    self.session_id,
                    self.turn_id,
                    self.initiating_user_id,
                )
            )
        if not valid:
            raise ValueError("actor requires explicit kind-specific provenance")
        return self


class AuditMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: (
        Literal[
            "CREATED",
            "PROVISIONING",
            "ARCHIVED",
            "ACTIVE",
            "INACTIVE",
            "SUSPENDED",
            "REVOKED",
            "DRAFT",
            "PUBLISHED",
            "OPEN",
            "CLOSED",
            "PENDING",
            "RUNNING",
            "PAUSED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
            "READY",
            "QUEUED",
            "CLAIMED",
            "ACCEPTED",
            "RELEASED",
            "PREPARED",
            "SCHEDULED",
            "ELIGIBLE",
            "SUPPRESSED",
            "DISPATCHED",
            "SUCCEEDED",
            "RECEIVED",
            "VERIFIED",
            "PLANNED",
            "APPROVED",
            "EXECUTING",
            "PARTIAL",
            "DENIED",
            "RETIRED",
            "AVAILABLE",
            "BUSY",
            "OFFLINE",
            "AMBIGUOUS",
        ]
        | None
    ) = None
    reason_code: (
        Literal[
            "AUTHORIZED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
            "REVOKED",
            "POLICY_DENIED",
            "TIMEOUT",
            "DEPENDENCY_UNAVAILABLE",
            "AMBIGUOUS",
        ]
        | None
    ) = None
    count: int | None = Field(default=None, ge=0, le=1_000_000, strict=True)
    version: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    related_id: UUID | None = None
    original_record_id: UUID | None = None


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@EVENT_REGISTRY.payload_model("audit.intent.recorded")
class AuditIntent(EventPayload):
    EVENT_TYPE: ClassVar[str] = "audit.intent.recorded"
    VERSION: ClassVar[int] = 1
    version: Literal[1] = 1
    scope: Literal["TENANT"] = "TENANT"
    organization_id: UUID
    source_id: UUID
    producer: str = Field(max_length=32)
    action: str = Field(max_length=80)
    target_type: str = Field(max_length=32)
    target_id: UUID
    actor: AuditActor
    occurred_at: dt.datetime
    outcome: Literal["SUCCESS", "FAILED", "DENIED", "AMBIGUOUS"] = "SUCCESS"
    metadata: AuditMetadata = Field(default_factory=AuditMetadata)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    request_id: UUID | None = None

    @field_validator("occurred_at")
    @classmethod
    def utc_time(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("audit time must be timezone aware")
        return value.astimezone(dt.UTC)

    @model_validator(mode="after")
    def source_contract(self) -> Self:
        if self.action not in ACTIONS.get(self.producer, frozenset()):
            raise ValueError("unregistered producer/action")
        if self.target_type not in TARGETS:
            raise ValueError("unregistered target type")
        if len(canonical(self.model_dump(mode="json"))) > 4096:
            raise ValueError("audit intent exceeds byte limit")
        return self


def semantic_digest(intent: AuditIntent) -> str:
    return hashlib.sha256(canonical(intent.model_dump(mode="json"))).hexdigest()


def record_digest(fact: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(fact)).hexdigest()
