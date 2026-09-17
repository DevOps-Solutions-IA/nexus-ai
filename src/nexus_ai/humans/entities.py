"""Strict domain and API contracts for Human Agent Operations."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

BoundedKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,62}$")]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,95}$")]
CorrelationId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]


class WorkItemState(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    ACCEPTED = "ACCEPTED"
    ACTIVE = "ACTIVE"
    AI_RETURN_PENDING = "AI_RETURN_PENDING"
    WRAP_UP = "WRAP_UP"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PresenceState(StrEnum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    AWAY = "AWAY"
    WRAP_UP = "WRAP_UP"


class OwnershipMode(StrEnum):
    AI = "AI"
    HUMAN = "HUMAN"
    UNASSIGNED = "UNASSIGNED"


class AssignmentState(StrEnum):
    CLAIMED = "CLAIMED"
    ACCEPTED = "ACCEPTED"
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    TRANSFERRED = "TRANSFERRED"
    COMPLETED = "COMPLETED"


class HandoffDirection(StrEnum):
    AI_TO_HUMAN = "AI_TO_HUMAN"
    HUMAN_TO_AI = "HUMAN_TO_AI"


class HandoffState(StrEnum):
    REQUESTED = "REQUESTED"
    AI_RETURN_PENDING = "AI_RETURN_PENDING"
    ACCEPTED = "ACCEPTED"
    P13_REJECTED = "P13_REJECTED"
    AMBIGUOUS = "AMBIGUOUS"
    COMPLETED = "COMPLETED"


class ActionAuthorizationState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


class HumanChannel(StrEnum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SMS = "SMS"
    VOICE = "VOICE"


class HumanQueue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    queue_key: str
    name: str
    enabled: bool
    supported_channels: tuple[HumanChannel, ...]
    required_skills: tuple[str, ...]
    max_active_assignments: int
    sla_target_seconds: int | None
    revision: int
    created_at: dt.datetime
    updated_at: dt.datetime


class AgentPresence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    agent_user_id: UUID
    state: PresenceState
    capacity: int
    active_assignment_count: int
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class HumanWorkItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    customer_id: UUID | None
    channel: HumanChannel
    source_type: str
    source_id: UUID | None
    call_id: UUID | None
    voice_session_id: UUID | None
    queue_id: UUID
    priority: int
    state: WorkItemState
    handoff_reason: str
    idempotency_key: str
    eligible_at: dt.datetime
    assigned_agent_id: UUID | None
    current_assignment_id: UUID | None
    lease_version: int
    queued_at: dt.datetime
    first_claim_at: dt.datetime | None
    accepted_at: dt.datetime | None
    first_response_at: dt.datetime | None
    completed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class HumanAssignment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    work_item_id: UUID
    queue_id: UUID
    owner_agent_id: UUID
    state: AssignmentState
    lease_version: int
    acquired_at: dt.datetime
    accepted_at: dt.datetime | None
    released_at: dt.datetime | None
    release_reason: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ClaimResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    work_item: HumanWorkItem
    assignment: HumanAssignment
    claim_token: str
    ownership_generation: int


class ConversationOwnership(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    mode: OwnershipMode
    ownership_generation: int
    work_item_id: UUID | None
    assignment_id: UUID | None
    agent_user_id: UUID | None
    ai_session_id: UUID | None
    updated_at: dt.datetime


class HumanHandoff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    work_item_id: UUID
    direction: HandoffDirection
    state: HandoffState
    idempotency_key: str
    semantic_fingerprint: str
    ownership_generation: int
    p13_idempotency_key: str | None
    p13_contract_version: str | None
    p13_session_id: UUID | None
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class HumanTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    actor_user_id: UUID | None
    action: str
    entity_type: str
    entity_id: UUID
    previous_state: str | None
    new_state: str
    reason_code: str
    correlation_id: str | None
    created_at: dt.datetime


class CreateQueueRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    queue_key: BoundedKey
    name: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    supported_channels: tuple[HumanChannel, ...]
    required_skills: tuple[Annotated[str, StringConstraints(min_length=1, max_length=48)], ...] = ()
    max_active_assignments: Annotated[int, Field(ge=1, le=100)] = 1
    sla_target_seconds: Annotated[int, Field(ge=1, le=604800)] | None = None

    @model_validator(mode="after")
    def _bounded_unique_sets(self) -> CreateQueueRequest:
        if not self.supported_channels or len(self.supported_channels) > 4:
            raise ValueError("supported_channels must contain between 1 and 4 values")
        if len(set(self.supported_channels)) != len(self.supported_channels):
            raise ValueError("supported_channels contains duplicates")
        if len(self.required_skills) > 32 or len(set(self.required_skills)) != len(
            self.required_skills
        ):
            raise ValueError("required_skills must be unique and contain at most 32 values")
        return self


class UpdateQueueRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_revision: Annotated[int, Field(ge=1)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=160)] | None = None
    enabled: bool | None = None
    supported_channels: tuple[HumanChannel, ...] | None = None
    required_skills: (
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=48)], ...] | None
    ) = None
    max_active_assignments: Annotated[int, Field(ge=1, le=100)] | None = None
    sla_target_seconds: Annotated[int, Field(ge=1, le=604800)] | None = None


class SetPresenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state: PresenceState
    capacity: Annotated[int, Field(ge=0, le=100)]
    expected_version: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def _offline_has_zero_capacity(self) -> SetPresenceRequest:
        if self.state is PresenceState.OFFLINE and self.capacity != 0:
            raise ValueError("OFFLINE presence requires zero capacity")
        return self


class QueueHumanWorkRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: UUID
    customer_id: UUID | None = None
    channel: HumanChannel
    queue_id: UUID
    priority: Annotated[int, Field(ge=-1000, le=1000)] = 0
    handoff_reason: ReasonCode
    source_type: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")] = "MANUAL"
    source_id: UUID | None = None
    call_id: UUID | None = None
    voice_session_id: UUID | None = None
    eligible_at: dt.datetime | None = None
    idempotency_key: IdempotencyKey
    correlation_id: CorrelationId | None = None


class ClaimWorkRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    queue_id: UUID
    expected_queue_revision: Annotated[int, Field(ge=1)]
    skills: tuple[Annotated[str, StringConstraints(min_length=1, max_length=48)], ...] = ()
    correlation_id: CorrelationId | None = None


class AssignmentActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    claim_token: Annotated[str, StringConstraints(min_length=32, max_length=256)]
    lease_version: Annotated[int, Field(ge=1)]
    ownership_generation: Annotated[int, Field(ge=1)]
    reason_code: ReasonCode = "HUMAN_ACTION"
    correlation_id: CorrelationId | None = None


class TransferToQueueRequest(AssignmentActionRequest):
    target_queue_id: UUID


class TransferToAgentRequest(AssignmentActionRequest):
    target_agent_user_id: UUID


class SupervisorActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reason_code: ReasonCode
    target_queue_id: UUID | None = None
    target_agent_user_id: UUID | None = None
    correlation_id: CorrelationId | None = None


class SupervisorTransferToAgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_agent_user_id: UUID
    reason_code: ReasonCode
    correlation_id: CorrelationId | None = None


class ReturnToAiRequest(AssignmentActionRequest):
    agent_id: UUID
    context: BoundedText
    idempotency_key: IdempotencyKey


class HumanSendRequest(AssignmentActionRequest):
    account_id: UUID
    to: tuple[Annotated[str, StringConstraints(min_length=1, max_length=320)], ...]
    content: BoundedText
    channel: HumanChannel
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def _bounded_recipients(self) -> HumanSendRequest:
        if not self.to or len(self.to) > 10:
            raise ValueError("human sends require between 1 and 10 recipients")
        if self.channel is HumanChannel.VOICE:
            raise ValueError("voice actions are references only and cannot use human messaging")
        return self


class CopilotRequest(AssignmentActionRequest):
    agent_id: UUID
    prompt: BoundedText
    idempotency_key: IdempotencyKey


class CopilotSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: UUID
    suggestion: str
    advisory_only: bool = True


class ActionAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    work_item_id: UUID
    assignment_id: UUID
    lease_version: int
    ownership_generation: int
    channel: HumanChannel
    semantic_fingerprint: str
    p09_idempotency_key: str
    state: ActionAuthorizationState
    message_id: UUID | None
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class PaginatedHumanResources(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[Any, ...]
    limit: int
    offset: int
