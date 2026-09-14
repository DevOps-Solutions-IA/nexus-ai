"""Strict Campaign API and durable-domain contracts."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from nexus_ai.campaigns.state_machine import (
    CampaignRunState,
    CampaignState,
    RecipientAttemptState,
    RecipientState,
    SendPermitState,
    SnapshotState,
)
from nexus_ai.messaging.entities import MessageChannel, MessageContent
from nexus_ai.scheduler.entities import (
    MisfirePolicy,
    RecurrenceSpec,
    ScheduleType,
)

CampaignKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class AudienceSourceType(StrEnum):
    EXPLICIT_CUSTOMERS = "EXPLICIT_CUSTOMERS"
    SAVED_SEGMENT = "SAVED_SEGMENT"
    IMPORT_ARTIFACT = "IMPORT_ARTIFACT"


class EligibilityReason(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DEFERRED_QUIET_HOURS = "DEFERRED_QUIET_HOURS"
    SUPPRESSED_GLOBAL = "SUPPRESSED_GLOBAL"
    SUPPRESSED_CAMPAIGN = "SUPPRESSED_CAMPAIGN"
    NO_CONSENT = "NO_CONSENT"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    CUSTOMER_INACTIVE = "CUSTOMER_INACTIVE"
    IDENTITY_INACTIVE = "IDENTITY_INACTIVE"
    INVALID_DESTINATION = "INVALID_DESTINATION"
    NO_CHANNEL = "NO_CHANNEL"
    DUPLICATE = "DUPLICATE"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    CANCELLED = "CANCELLED"


class SuppressionScope(StrEnum):
    GLOBAL = "GLOBAL"
    CAMPAIGN = "CAMPAIGN"
    CUSTOMER = "CUSTOMER"
    IDENTITY = "IDENTITY"


class QuietHoursPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "UTC"
    start_local: dt.time = dt.time(21, 0)
    end_local: dt.time = dt.time(8, 0)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA identifier") from exc
        return value

    @field_validator("start_local", "end_local")
    @classmethod
    def _naive_time(cls, value: dt.time) -> dt.time:
        if value.tzinfo is not None:
            raise ValueError("quiet-hour wall times must be timezone-naive")
        return value


class ThrottlePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recipients_per_tick: Annotated[int, Field(ge=1, le=500)] = 100
    messages_per_minute: Annotated[int, Field(ge=1, le=100_000)] = 60
    campaign_limit: Annotated[int, Field(ge=1, le=10_000_000)] = 100_000
    organization_messages_per_minute: Annotated[int, Field(ge=1, le=1_000_000)] = 10_000


class AudienceMemberInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: UUID
    identity_id: UUID
    conversation_id: UUID


class CampaignDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_workflow_version_id: UUID
    recipient_workflow_version_id: UUID
    account_id: UUID
    channel: MessageChannel
    content: MessageContent
    subject: Annotated[str, StringConstraints(max_length=255)] | None = None
    audience_source: AudienceSourceType = AudienceSourceType.EXPLICIT_CUSTOMERS
    audience: tuple[AudienceMemberInput, ...]
    source_reference: Annotated[str, StringConstraints(max_length=200)] | None = None
    quiet_hours: QuietHoursPolicy = QuietHoursPolicy()
    throttle: ThrottlePolicy = ThrottlePolicy()

    @model_validator(mode="after")
    def _bounded(self) -> CampaignDraft:
        if self.audience_source is AudienceSourceType.EXPLICIT_CUSTOMERS:
            if not self.audience:
                raise ValueError("explicit audiences require at least one member")
            if len(self.audience) > 500:
                raise ValueError("explicit audience is limited to 500 members per campaign")
        elif self.audience or self.source_reference is None:
            raise ValueError("governed segment/import sources require only source_reference")
        if len({(m.customer_id, self.channel) for m in self.audience}) != len(self.audience):
            raise ValueError("duplicate audience member")
        if len(self.audience) > self.throttle.campaign_limit:
            raise ValueError("audience exceeds campaign_limit")
        if self.channel is not MessageChannel.EMAIL and self.subject is not None:
            raise ValueError("only EMAIL campaigns accept a subject")
        if len(self.model_dump_json()) > 1_048_576:
            raise ValueError("campaign draft exceeds 1 MiB")
        return self


class CreateCampaignRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_key: CampaignKey
    name: Name
    draft: CampaignDraft


class UpdateCampaignRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_revision: Annotated[int, Field(ge=1)]
    name: Name | None = None
    draft: CampaignDraft | None = None


class ScheduleCampaignRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_type: ScheduleType = ScheduleType.ONE_TIME
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    start_at: dt.datetime
    end_at: dt.datetime | None = None
    recurrence: RecurrenceSpec | None = None
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    max_catch_up: Annotated[int, Field(ge=1, le=100)] = 1

    @field_validator("timezone")
    @classmethod
    def _schedule_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA identifier") from exc
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def _schedule_aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return None if value is None else value.astimezone(dt.UTC)

    @model_validator(mode="after")
    def _schedule_shape(self) -> ScheduleCampaignRequest:
        if self.schedule_type is ScheduleType.ONE_TIME and self.recurrence is not None:
            raise ValueError("one-time schedules cannot include recurrence")
        if self.schedule_type is ScheduleType.RECURRING and self.recurrence is None:
            raise ValueError("recurring schedules require recurrence")
        if self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("end_at must not precede start_at")
        if self.misfire_policy is not MisfirePolicy.CATCH_UP_BOUNDED and self.max_catch_up != 1:
            raise ValueError("max_catch_up applies only to CATCH_UP_BOUNDED")
        return self


class ContactPreferenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: UUID
    identity_id: UUID
    channel: MessageChannel
    consent_granted: bool
    unsubscribed: bool = False
    do_not_contact: bool = False
    evidence_ref: Annotated[str, StringConstraints(max_length=200)] | None = None

    @model_validator(mode="after")
    def _affirmative_consent_evidence(self) -> ContactPreferenceRequest:
        if self.consent_granted and not self.evidence_ref:
            raise ValueError("affirmative campaign consent requires evidence_ref")
        if (self.unsubscribed or self.do_not_contact) and self.consent_granted:
            raise ValueError("withdrawn contact permission cannot remain granted")
        return self


class SuppressionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: MessageChannel
    scope: SuppressionScope
    campaign_id: UUID | None = None
    customer_id: UUID | None = None
    identity_id: UUID | None = None
    reason_code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]

    @model_validator(mode="after")
    def _scope_reference(self) -> SuppressionRequest:
        references = {
            SuppressionScope.CAMPAIGN: self.campaign_id,
            SuppressionScope.CUSTOMER: self.customer_id,
            SuppressionScope.IDENTITY: self.identity_id,
        }
        supplied = [
            reference
            for reference in (self.campaign_id, self.customer_id, self.identity_id)
            if reference is not None
        ]
        if self.scope is SuppressionScope.GLOBAL and supplied:
            raise ValueError("global suppression cannot carry a resource reference")
        if self.scope is not SuppressionScope.GLOBAL and references[self.scope] is None:
            raise ValueError("suppression scope requires its matching reference")
        if self.scope is not SuppressionScope.GLOBAL and len(supplied) != 1:
            raise ValueError("suppression accepts only its matching scope reference")
        return self


class Campaign(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    campaign_key: str
    name: str
    state: CampaignState
    revision: int
    draft: CampaignDraft
    prepared_revision_id: UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime


class CampaignRevision(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    campaign_id: UUID
    revision_number: int
    content_hash: str
    specification: CampaignDraft
    published_at: dt.datetime


class AudienceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    campaign_revision_id: UUID
    state: SnapshotState
    source_type: AudienceSourceType
    source_digest: str
    cursor: int
    resolved_count: int
    eligible_count: int
    rejected_count: int
    sealed_at: dt.datetime | None
    created_at: dt.datetime


class CampaignRecipient(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    snapshot_id: UUID
    customer_id: UUID
    identity_id: UUID
    conversation_id: UUID
    channel: MessageChannel
    destination_fingerprint: str
    state: RecipientState
    eligibility_reason: EligibilityReason
    next_eligible_at: dt.datetime | None
    evaluated_consent_epoch: int
    evaluated_suppression_epoch: int
    created_at: dt.datetime


class CampaignRun(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    campaign_id: UUID
    campaign_revision_id: UUID
    audience_snapshot_id: UUID
    state: CampaignRunState
    idempotency_key: str
    release_workflow_run_id: UUID | None
    release_schedule_occurrence_id: UUID | None
    schedule_id: UUID | None
    claimed_count: int
    dispatched_count: int
    suppressed_count: int
    failed_count: int
    authorized_count: int
    attempt_materialization_cursor: int
    attempt_materialization_complete: bool
    cancellation_processed_count: int
    cancellation_complete: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class RecipientAttempt(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    campaign_run_id: UUID
    recipient_id: UUID
    state: RecipientAttemptState
    attempt_number: int
    owner_id: UUID | None
    claim_token: UUID | None
    claimed_at: dt.datetime | None
    p14_idempotency_key: str
    workflow_run_id: UUID | None
    p09_idempotency_key: str
    message_id: UUID | None
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class RecipientClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    run: CampaignRun
    recipient: CampaignRecipient
    attempt: RecipientAttempt
    owner_id: UUID
    claim_token: UUID


class SendPermit(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    recipient_attempt_id: UUID
    state: SendPermitState
    permit_token: UUID
    consent_epoch: int
    suppression_epoch: int
    p09_idempotency_key: str
    authorized_at: dt.datetime
    consumed_at: dt.datetime | None
    message_id: UUID | None


class CampaignTransition(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    organization_id: UUID
    entity_type: str
    entity_id: UUID
    from_state: str | None
    to_state: str
    reason_code: str
    correlation_id: str | None
    created_at: dt.datetime
