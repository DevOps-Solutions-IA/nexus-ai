"""Durable Scheduler orchestration; P14 is the only dispatch boundary."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.domain.scheduler.repository import SchedulerRepository, _schedule
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    MisfirePolicy,
    OccurrenceClaim,
    Schedule,
    ScheduleOccurrence,
    ScheduleTransition,
    ScheduleType,
    UpdateScheduleRequest,
)
from nexus_ai.scheduler.errors import (
    OccurrenceNotFoundError,
    ScheduleConflictError,
    ScheduleDispatchFailedError,
    ScheduleInvalidStateError,
    ScheduleNotFoundError,
)
from nexus_ai.scheduler.recurrence import (
    MAX_SEARCH_STEPS,
    TemporalSlot,
    first_slot,
    next_slot,
    timezone_data_version,
)
from nexus_ai.scheduler.state_machine import OccurrenceState, ScheduleState
from nexus_ai.workflows.entities import StartWorkflowRunRequest
from nexus_ai.workflows.service import WorkflowService

DEFAULT_BATCH = 100
MAX_BATCH = 500


class SchedulerService:
    def __init__(
        self,
        database: Database,
        publisher: EventPublisher,
        workflows: WorkflowService,
        *,
        service_name: str,
    ) -> None:
        self._db = database
        self._publisher = publisher
        self._workflows = workflows
        self._service_name = service_name

    async def create_schedule(
        self, organization_id: uuid.UUID, request: CreateScheduleRequest
    ) -> Schedule:
        await self._workflows.get_version(organization_id, request.workflow_version_id)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                repo = SchedulerRepository(tenant)
                schedule = await repo.create_schedule(
                    correlation_id=self._correlation_id(),
                    schedule_key=request.schedule_key,
                    target_type=request.target_type,
                    workflow_version_id=request.workflow_version_id,
                    schedule_type=request.schedule_type.value,
                    timezone=request.timezone,
                    recurrence_spec=(
                        None
                        if request.recurrence is None
                        else request.recurrence.model_dump(mode="json")
                    ),
                    input_payload=request.input,
                    start_at=request.start_at,
                    end_at=request.end_at,
                    next_fire_at=None,
                    next_local_time=None,
                    state=ScheduleState.DRAFT.value,
                    misfire_policy=request.misfire_policy.value,
                    max_catch_up=request.max_catch_up,
                    revision=1,
                    timezone_data_version=timezone_data_version(),
                )
                await self._event(
                    tenant.session, organization_id, "scheduler.schedule.created", schedule
                )
                return schedule
        except IntegrityError as exc:
            raise ScheduleConflictError("schedule_key already exists in this Organization") from exc

    async def get_schedule(self, organization_id: uuid.UUID, schedule_id: uuid.UUID) -> Schedule:
        async with self._db.tenant_transaction(organization_id) as tenant:
            result = await SchedulerRepository(tenant).schedule(schedule_id)
            if result is None:
                raise ScheduleNotFoundError()
            return result

    async def list_schedules(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[Schedule]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await SchedulerRepository(tenant).list_schedules(limit=limit, offset=offset)

    async def update_schedule(
        self,
        organization_id: uuid.UUID,
        schedule_id: uuid.UUID,
        request: UpdateScheduleRequest,
    ) -> Schedule:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.schedule_row(schedule_id, for_update=True)
            if row is None:
                raise ScheduleNotFoundError()
            if row.state not in {ScheduleState.DRAFT.value, ScheduleState.PAUSED.value}:
                raise ScheduleInvalidStateError("only DRAFT or PAUSED schedules may be edited")
            if row.revision != request.expected_revision:
                raise ScheduleConflictError("schedule revision changed; reload before editing")
            values = request.model_dump(exclude_unset=True)
            values.pop("expected_revision", None)
            current = _schedule(row)
            candidate = CreateScheduleRequest.model_validate(
                {
                    "schedule_key": current.schedule_key,
                    "workflow_version_id": current.workflow_version_id,
                    "schedule_type": current.schedule_type,
                    "timezone": values.get("timezone", current.timezone),
                    "start_at": values.get("start_at", current.start_at),
                    "end_at": values.get("end_at", current.end_at),
                    "recurrence": values.get("recurrence", current.recurrence),
                    "input": values.get("input", current.input),
                    "misfire_policy": values.get("misfire_policy", current.misfire_policy),
                    "max_catch_up": values.get("max_catch_up", current.max_catch_up),
                }
            )
            row.timezone = candidate.timezone
            row.start_at = candidate.start_at
            row.end_at = candidate.end_at
            row.recurrence_spec = (
                None
                if candidate.recurrence is None
                else candidate.recurrence.model_dump(mode="json")
            )
            row.input_payload = candidate.input
            row.misfire_policy = candidate.misfire_policy.value
            row.max_catch_up = candidate.max_catch_up
            row.revision += 1
            row.next_fire_at = None
            row.next_local_time = None
            row.updated_at = await repo.database_now()
            await tenant.session.flush()
            return _schedule(row)

    async def activate_schedule(
        self, organization_id: uuid.UUID, schedule_id: uuid.UUID
    ) -> Schedule:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.schedule_row(schedule_id, for_update=True)
            if row is None:
                raise ScheduleNotFoundError()
            if row.state != ScheduleState.DRAFT.value:
                raise ScheduleInvalidStateError("only a DRAFT schedule may be activated")
            schedule = _schedule(row)
            slot = first_slot(
                start_at=schedule.start_at,
                timezone=schedule.timezone,
                recurrence=schedule.recurrence,
            )
            if schedule.end_at is not None and slot.scheduled_for > schedule.end_at:
                raise ScheduleInvalidStateError("schedule has no occurrence within its time window")
            row.next_fire_at = slot.scheduled_for
            row.next_local_time = slot.intended_local_time
            result = await repo.transition_schedule(
                row,
                ScheduleState.ACTIVE,
                "SCHEDULE_ACTIVATED",
                correlation_id=self._correlation_id(),
            )
            await self._event(
                tenant.session, organization_id, "scheduler.schedule.activated", result
            )
            return result

    async def pause_schedule(self, organization_id: uuid.UUID, schedule_id: uuid.UUID) -> Schedule:
        return await self._transition(
            organization_id, schedule_id, ScheduleState.PAUSED, "SCHEDULE_PAUSED"
        )

    async def resume_schedule(self, organization_id: uuid.UUID, schedule_id: uuid.UUID) -> Schedule:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.schedule_row(schedule_id, for_update=True)
            if row is None:
                raise ScheduleNotFoundError()
            if row.state != ScheduleState.PAUSED.value:
                raise ScheduleInvalidStateError("only a PAUSED schedule may be resumed")
            schedule = _schedule(row)
            if row.next_fire_at is None or row.next_local_time is None:
                slot = first_slot(
                    start_at=schedule.start_at,
                    timezone=schedule.timezone,
                    recurrence=schedule.recurrence,
                )
                row.next_fire_at = slot.scheduled_for
                row.next_local_time = slot.intended_local_time
            result = await repo.transition_schedule(
                row,
                ScheduleState.ACTIVE,
                "SCHEDULE_RESUMED",
                correlation_id=self._correlation_id(),
            )
            await self._event(tenant.session, organization_id, "scheduler.schedule.resumed", result)
            return result

    async def cancel_schedule(self, organization_id: uuid.UUID, schedule_id: uuid.UUID) -> Schedule:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.schedule_row(schedule_id, for_update=True)
            if row is None:
                raise ScheduleNotFoundError()
            result = await repo.transition_schedule(
                row,
                ScheduleState.CANCELLED,
                "SCHEDULE_CANCELLED",
                correlation_id=self._correlation_id(),
            )
            cancelled = await repo.cancel_pending(
                schedule_id, correlation_id=self._correlation_id()
            )
            await self._event(
                tenant.session, organization_id, "scheduler.schedule.cancelled", result
            )
            for occurrence in cancelled:
                await self._occurrence_event(
                    tenant.session, organization_id, "scheduler.occurrence.cancelled", occurrence
                )
            return result

    async def _transition(
        self, organization_id: uuid.UUID, schedule_id: uuid.UUID, target: ScheduleState, reason: str
    ) -> Schedule:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.schedule_row(schedule_id, for_update=True)
            if row is None:
                raise ScheduleNotFoundError()
            result = await repo.transition_schedule(
                row, target, reason, correlation_id=self._correlation_id()
            )
            await self._event(
                tenant.session,
                organization_id,
                f"scheduler.schedule.{target.value.lower()}",
                result,
            )
            return result

    async def materialize_due(
        self, organization_id: uuid.UUID, *, limit: int = DEFAULT_BATCH
    ) -> list[ScheduleOccurrence]:
        if not 1 <= limit <= MAX_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_BATCH}")
        materialized: list[ScheduleOccurrence] = []
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            now = await repo.database_now()
            rows = await repo.due_schedule_rows(limit=limit)
            remaining = limit
            for row in rows:
                if remaining <= 0:
                    break
                schedule = _schedule(row)
                slots, next_future = self._elapsed_slots(schedule, now)
                selected: list[TemporalSlot]
                terminal_state = OccurrenceState.PENDING
                reason = "OCCURRENCE_CREATED"
                if schedule.misfire_policy is MisfirePolicy.SKIP:
                    selected = slots[-1:]
                    terminal_state = OccurrenceState.SKIPPED
                    reason = "MISFIRE_SKIPPED"
                elif schedule.misfire_policy is MisfirePolicy.FIRE_ONCE:
                    selected = slots[-1:]
                else:
                    selected = slots[: schedule.max_catch_up]
                for slot in selected[:remaining]:
                    state = terminal_state
                    slot_reason = reason
                    if slot.nonexistent:
                        state = OccurrenceState.SKIPPED
                        slot_reason = "DST_NONEXISTENT_LOCAL_TIME"
                    occurrence = await repo.add_occurrence(
                        row,
                        slot,
                        state=state,
                        reason=slot_reason,
                        correlation_id=self._correlation_id(),
                    )
                    materialized.append(occurrence)
                    await self._occurrence_event(
                        tenant.session,
                        organization_id,
                        "scheduler.occurrence.skipped"
                        if state is OccurrenceState.SKIPPED
                        else "scheduler.occurrence.created",
                        occurrence,
                    )
                    remaining -= 1
                if schedule.schedule_type is ScheduleType.ONE_TIME:
                    row.next_fire_at = None
                    row.next_local_time = None
                elif next_future is None:
                    row.next_fire_at = None
                    row.next_local_time = None
                    await repo.transition_schedule(
                        row,
                        ScheduleState.COMPLETED,
                        "SCHEDULE_EXHAUSTED",
                        source="ENGINE",
                        correlation_id=self._correlation_id(),
                    )
                else:
                    row.next_fire_at = next_future.scheduled_for
                    row.next_local_time = next_future.intended_local_time
            await tenant.session.flush()
        return materialized

    @staticmethod
    def _elapsed_slots(
        schedule: Schedule, now: dt.datetime
    ) -> tuple[list[TemporalSlot], TemporalSlot | None]:
        if schedule.next_fire_at is None or schedule.next_local_time is None:
            return [], None
        current = TemporalSlot(
            schedule.next_local_time,
            schedule.next_fire_at,
            int(
                (
                    schedule.next_fire_at.astimezone(ZoneInfo(schedule.timezone)).utcoffset()
                    or dt.timedelta()
                ).total_seconds()
            ),
            0,
            schedule.next_fire_at.astimezone(ZoneInfo(schedule.timezone)).replace(tzinfo=None)
            != schedule.next_local_time,
        )
        due: list[TemporalSlot] = []
        for _ in range(MAX_SEARCH_STEPS):
            if current.scheduled_for > now:
                return due, current
            if schedule.end_at is not None and current.scheduled_for > schedule.end_at:
                return due, None
            due.append(current)
            if schedule.recurrence is None:
                return due, None
            current = next_slot(
                current.intended_local_time,
                current.scheduled_for,
                schedule.timezone,
                schedule.recurrence,
            )
        raise ScheduleInvalidStateError("recurrence materialization exceeded bounded search")

    async def list_occurrences(
        self, organization_id: uuid.UUID, schedule_id: uuid.UUID, *, limit: int
    ) -> list[ScheduleOccurrence]:
        await self.get_schedule(organization_id, schedule_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await SchedulerRepository(tenant).occurrences(schedule_id, limit=limit)

    async def get_occurrence(
        self, organization_id: uuid.UUID, occurrence_id: uuid.UUID
    ) -> ScheduleOccurrence:
        async with self._db.tenant_transaction(organization_id) as tenant:
            result = await SchedulerRepository(tenant).occurrence(occurrence_id)
            if result is None:
                raise OccurrenceNotFoundError()
            return result

    async def cancel_occurrence(
        self, organization_id: uuid.UUID, occurrence_id: uuid.UUID
    ) -> ScheduleOccurrence:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.occurrence_row(occurrence_id, for_update=True)
            if row is None:
                raise OccurrenceNotFoundError()
            result = await repo.cancel_occurrence(row, correlation_id=self._correlation_id())
            await self._occurrence_event(
                tenant.session, organization_id, "scheduler.occurrence.cancelled", result
            )
            return result

    async def claim_due(
        self, organization_id: uuid.UUID, owner_id: uuid.UUID
    ) -> OccurrenceClaim | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            claim = await SchedulerRepository(tenant).claim_due(
                owner_id, correlation_id=self._correlation_id()
            )
            if claim is not None:
                await self._occurrence_event(
                    tenant.session,
                    organization_id,
                    "scheduler.occurrence.claimed",
                    claim.occurrence,
                )
            return claim

    async def dispatch_claim(
        self, organization_id: uuid.UUID, claim: OccurrenceClaim
    ) -> ScheduleOccurrence:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await SchedulerRepository(tenant).begin_dispatch(claim)
        try:
            run = await self._workflows.start_run(
                organization_id,
                StartWorkflowRunRequest(
                    workflow_version_id=claim.occurrence.workflow_version_id,
                    input=claim.workflow_input,
                    idempotency_key=claim.occurrence.p14_idempotency_key,
                    correlation_id=self._correlation_id(),
                ),
            )
        except NxsError as exc:
            async with self._db.tenant_transaction(organization_id) as tenant:
                failed = await SchedulerRepository(tenant).fail_dispatch(
                    claim, exc.code, correlation_id=self._correlation_id()
                )
                await self._occurrence_event(
                    tenant.session, organization_id, "scheduler.occurrence.failed", failed
                )
            raise ScheduleDispatchFailedError("P14 rejected the scheduled workflow start") from exc
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = SchedulerRepository(tenant)
            result = await repo.finish_dispatch(
                claim, run.id, correlation_id=self._correlation_id()
            )
            await self._occurrence_event(
                tenant.session, organization_id, "scheduler.occurrence.dispatched", result
            )
            return result

    async def transitions(
        self, organization_id: uuid.UUID, entity_id: uuid.UUID, *, limit: int
    ) -> list[ScheduleTransition]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await SchedulerRepository(tenant).transitions(entity_id, limit=limit)

    async def _event(
        self, session: Any, organization_id: uuid.UUID, event_type: str, schedule: Schedule
    ) -> None:
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="scheduler_schedule",
            aggregate_id=str(schedule.id),
            producer=self._service_name,
            organization_id=organization_id,
            correlation_id=self._correlation_id(),
            payload={
                "schedule_id": str(schedule.id),
                "state": schedule.state.value,
                "reason_code": None,
            },
        )
        await self._publisher.enqueue(session, envelope)

    async def _occurrence_event(
        self,
        session: Any,
        organization_id: uuid.UUID,
        event_type: str,
        occurrence: ScheduleOccurrence,
    ) -> None:
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="scheduler_occurrence",
            aggregate_id=str(occurrence.id),
            producer=self._service_name,
            organization_id=organization_id,
            correlation_id=self._correlation_id(),
            payload={
                "schedule_id": str(occurrence.schedule_id),
                "occurrence_id": str(occurrence.id),
                "scheduled_for": occurrence.scheduled_for.isoformat(),
                "state": occurrence.state.value,
                "reason_code": occurrence.error_code,
            },
        )
        await self._publisher.enqueue(session, envelope)

    @staticmethod
    def _correlation_id() -> str | None:
        context = current_context()
        return None if context is None else context.correlation_id
