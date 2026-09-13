"""Strict API and durable-domain contracts for NXS-P15."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Literal
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

from nexus_ai.scheduler.state_machine import OccurrenceState, ScheduleState

ScheduleKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
CorrelationId = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ScheduleTargetType(StrEnum):
    START_WORKFLOW = "START_WORKFLOW"


class ScheduleType(StrEnum):
    ONE_TIME = "ONE_TIME"
    RECURRING = "RECURRING"


class RecurrenceFrequency(StrEnum):
    MINUTELY = "MINUTELY"
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class MisfirePolicy(StrEnum):
    SKIP = "SKIP"
    FIRE_ONCE = "FIRE_ONCE"
    CATCH_UP_BOUNDED = "CATCH_UP_BOUNDED"


class RecurrenceSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    frequency: RecurrenceFrequency
    interval: Annotated[int, Field(ge=1, le=10_000)] = 1
    weekdays: tuple[Annotated[int, Field(ge=0, le=6)], ...] = ()
    month_days: tuple[Annotated[int, Field(ge=1, le=31)], ...] = ()
    local_time: dt.time | None = None

    @model_validator(mode="after")
    def _coherent(self) -> RecurrenceSpec:
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must be unique")
        if len(set(self.month_days)) != len(self.month_days):
            raise ValueError("month_days must be unique")
        if self.frequency in {RecurrenceFrequency.MINUTELY, RecurrenceFrequency.HOURLY}:
            if self.weekdays or self.month_days or self.local_time is not None:
                raise ValueError("minute/hour recurrence does not accept calendar selectors")
        elif self.frequency is RecurrenceFrequency.DAILY:
            if self.local_time is None or self.weekdays or self.month_days:
                raise ValueError("daily recurrence requires only local_time")
        elif self.frequency is RecurrenceFrequency.WEEKLY:
            if self.local_time is None or not self.weekdays or self.month_days:
                raise ValueError("weekly recurrence requires weekdays and local_time")
        elif self.local_time is None or not self.month_days or self.weekdays:
            raise ValueError("monthly recurrence requires month_days and local_time")
        if self.local_time is not None and self.local_time.tzinfo is not None:
            raise ValueError("local_time must not contain timezone information")
        return self


class CreateScheduleRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_key: ScheduleKey
    target_type: Literal["START_WORKFLOW"] = "START_WORKFLOW"
    workflow_version_id: UUID
    schedule_type: ScheduleType
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    start_at: dt.datetime
    end_at: dt.datetime | None = None
    recurrence: RecurrenceSpec | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    max_catch_up: Annotated[int, Field(ge=1, le=100)] = 1
    correlation_id: CorrelationId | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA identifier") from exc
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def _aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return None if value is None else value.astimezone(dt.UTC)

    @model_validator(mode="after")
    def _shape(self) -> CreateScheduleRequest:
        if self.schedule_type is ScheduleType.ONE_TIME and self.recurrence is not None:
            raise ValueError("one-time schedules cannot include recurrence")
        if self.schedule_type is ScheduleType.RECURRING and self.recurrence is None:
            raise ValueError("recurring schedules require recurrence")
        if self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("end_at must not precede start_at")
        if self.misfire_policy is not MisfirePolicy.CATCH_UP_BOUNDED and self.max_catch_up != 1:
            raise ValueError("max_catch_up applies only to CATCH_UP_BOUNDED")
        if len(self.model_dump_json()) > 65_536:
            raise ValueError("schedule payload exceeds 64 KiB")
        return self


class UpdateScheduleRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_revision: Annotated[int, Field(ge=1)]
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    start_at: dt.datetime | None = None
    end_at: dt.datetime | None = None
    recurrence: RecurrenceSpec | None = None
    input: dict[str, Any] | None = None
    misfire_policy: MisfirePolicy | None = None
    max_catch_up: Annotated[int, Field(ge=1, le=100)] | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA identifier") from exc
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def _aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return None if value is None else value.astimezone(dt.UTC)


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    schedule_key: str
    target_type: ScheduleTargetType
    workflow_version_id: UUID
    schedule_type: ScheduleType
    timezone: str
    recurrence: RecurrenceSpec | None
    input: dict[str, Any]
    start_at: dt.datetime
    end_at: dt.datetime | None
    next_fire_at: dt.datetime | None
    next_local_time: dt.datetime | None
    state: ScheduleState
    misfire_policy: MisfirePolicy
    max_catch_up: int
    revision: int
    timezone_data_version: str
    created_at: dt.datetime
    updated_at: dt.datetime


class ScheduleOccurrence(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    schedule_id: UUID
    workflow_version_id: UUID
    schedule_revision: int
    occurrence_key: str
    scheduled_for: dt.datetime
    intended_local_time: dt.datetime
    timezone: str
    utc_offset_seconds: int
    fold: int
    timezone_data_version: str
    misfire_policy: MisfirePolicy
    state: OccurrenceState
    claim_owner_id: UUID | None
    claim_token: UUID | None
    claimed_at: dt.datetime | None
    dispatch_started_at: dt.datetime | None
    dispatch_attempt_count: int
    p14_idempotency_key: str
    workflow_run_id: UUID | None
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ScheduleTransition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    entity_type: str
    entity_id: UUID
    from_state: str | None
    to_state: str
    reason_code: str
    source: str
    correlation_id: str | None
    created_at: dt.datetime


class OccurrenceClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    occurrence: ScheduleOccurrence
    owner_id: UUID
    claim_token: UUID
    workflow_input: dict[str, Any]
