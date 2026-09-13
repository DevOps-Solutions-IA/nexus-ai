"""Tenant-scoped PostgreSQL Scheduler repository and fencing primitives."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Select, func, select, update

from nexus_ai.domain.scheduler.models import (
    SchedulerOccurrenceRecord,
    SchedulerScheduleRecord,
    SchedulerTransitionHistoryRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.scheduler.entities import (
    OccurrenceClaim,
    RecurrenceSpec,
    Schedule,
    ScheduleOccurrence,
    ScheduleTransition,
)
from nexus_ai.scheduler.errors import ScheduleExecutionFencedError
from nexus_ai.scheduler.identity import build_occurrence_key
from nexus_ai.scheduler.recurrence import TemporalSlot
from nexus_ai.scheduler.state_machine import (
    OccurrenceState,
    ScheduleState,
    require_occurrence_transition,
    require_schedule_transition,
)


def _schedule(row: SchedulerScheduleRecord) -> Schedule:
    return Schedule(
        id=row.id,
        organization_id=row.organization_id,
        schedule_key=row.schedule_key,
        target_type=row.target_type,
        workflow_version_id=row.workflow_version_id,
        schedule_type=row.schedule_type,
        timezone=row.timezone,
        recurrence=None
        if row.recurrence_spec is None
        else RecurrenceSpec.model_validate(row.recurrence_spec),
        input=dict(row.input_payload),
        start_at=row.start_at,
        end_at=row.end_at,
        next_fire_at=row.next_fire_at,
        next_local_time=row.next_local_time,
        state=row.state,
        misfire_policy=row.misfire_policy,
        max_catch_up=row.max_catch_up,
        revision=row.revision,
        timezone_data_version=row.timezone_data_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _occurrence(row: SchedulerOccurrenceRecord) -> ScheduleOccurrence:
    return ScheduleOccurrence(
        id=row.id,
        organization_id=row.organization_id,
        schedule_id=row.schedule_id,
        workflow_version_id=row.workflow_version_id,
        schedule_revision=row.schedule_revision,
        occurrence_key=row.occurrence_key,
        scheduled_for=row.scheduled_for,
        intended_local_time=row.intended_local_time,
        timezone=row.timezone,
        utc_offset_seconds=row.utc_offset_seconds,
        fold=row.fold,
        timezone_data_version=row.timezone_data_version,
        misfire_policy=row.misfire_policy,
        state=row.state,
        claim_owner_id=row.claim_owner_id,
        claim_token=row.claim_token,
        claimed_at=row.claimed_at,
        dispatch_started_at=row.dispatch_started_at,
        dispatch_attempt_count=row.dispatch_attempt_count,
        p14_idempotency_key=row.p14_idempotency_key,
        workflow_run_id=row.workflow_run_id,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SchedulerRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._session = tenant.session
        self._org = tenant.organization_id

    async def database_now(self) -> dt.datetime:
        value = (await self._session.execute(select(func.clock_timestamp()))).scalar_one()
        if not isinstance(value, dt.datetime):
            raise RuntimeError("database clock returned an invalid value")
        return value

    async def create_schedule(
        self, *, correlation_id: str | None = None, **values: Any
    ) -> Schedule:
        row = SchedulerScheduleRecord(id=uuid.uuid7(), organization_id=self._org, **values)
        self._session.add(row)
        await self._session.flush()
        await self.add_transition(
            "SCHEDULE",
            row.id,
            None,
            row.state,
            "SCHEDULE_CREATED",
            "API",
            correlation_id,
        )
        return _schedule(row)

    async def schedule_row(
        self, schedule_id: uuid.UUID, *, for_update: bool = False
    ) -> SchedulerScheduleRecord | None:
        query: Select[tuple[SchedulerScheduleRecord]] = select(SchedulerScheduleRecord).where(
            SchedulerScheduleRecord.organization_id == self._org,
            SchedulerScheduleRecord.id == schedule_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def schedule(self, schedule_id: uuid.UUID) -> Schedule | None:
        row = await self.schedule_row(schedule_id)
        return None if row is None else _schedule(row)

    async def list_schedules(self, *, limit: int, offset: int) -> list[Schedule]:
        rows = (
            (
                await self._session.execute(
                    select(SchedulerScheduleRecord)
                    .where(SchedulerScheduleRecord.organization_id == self._org)
                    .order_by(SchedulerScheduleRecord.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_schedule(row) for row in rows]

    async def transition_schedule(
        self,
        row: SchedulerScheduleRecord,
        target: ScheduleState,
        reason: str,
        *,
        source: str = "API",
        correlation_id: str | None = None,
    ) -> Schedule:
        previous = ScheduleState(row.state)
        require_schedule_transition(previous, target)
        now = await self.database_now()
        row.state = target.value
        row.revision += 1
        row.updated_at = now
        await self.add_transition(
            "SCHEDULE", row.id, previous.value, target.value, reason, source, correlation_id
        )
        await self._session.flush()
        return _schedule(row)

    async def due_schedule_rows(self, *, limit: int) -> list[SchedulerScheduleRecord]:
        return list(
            (
                await self._session.execute(
                    select(SchedulerScheduleRecord)
                    .where(
                        SchedulerScheduleRecord.organization_id == self._org,
                        SchedulerScheduleRecord.state == ScheduleState.ACTIVE.value,
                        SchedulerScheduleRecord.next_fire_at.is_not(None),
                        SchedulerScheduleRecord.next_fire_at <= func.clock_timestamp(),
                    )
                    .order_by(SchedulerScheduleRecord.next_fire_at, SchedulerScheduleRecord.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

    async def add_occurrence(
        self,
        schedule: SchedulerScheduleRecord,
        slot: TemporalSlot,
        *,
        state: OccurrenceState,
        reason: str,
        correlation_id: str | None = None,
    ) -> ScheduleOccurrence:
        occurrence_id = uuid.uuid7()
        occurrence_key = build_occurrence_key(
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            timezone=schedule.timezone,
            intended_local_time=slot.intended_local_time,
            fold=slot.fold,
        )
        row = SchedulerOccurrenceRecord(
            id=occurrence_id,
            organization_id=self._org,
            schedule_id=schedule.id,
            workflow_version_id=schedule.workflow_version_id,
            schedule_revision=schedule.revision,
            occurrence_key=occurrence_key,
            scheduled_for=slot.scheduled_for,
            intended_local_time=slot.intended_local_time,
            timezone=schedule.timezone,
            utc_offset_seconds=slot.utc_offset_seconds,
            fold=slot.fold,
            timezone_data_version=schedule.timezone_data_version,
            misfire_policy=schedule.misfire_policy,
            state=state.value,
            claim_owner_id=None,
            claim_token=None,
            dispatch_attempt_count=0,
            p14_idempotency_key=f"schedule:{schedule.id}:{occurrence_id}",
            workflow_run_id=None,
            workflow_input=dict(schedule.input_payload),
            error_code=reason if state is OccurrenceState.SKIPPED else None,
        )
        self._session.add(row)
        await self._session.flush()
        await self.add_transition(
            "OCCURRENCE", row.id, None, state.value, reason, "ENGINE", correlation_id
        )
        return _occurrence(row)

    async def occurrences(self, schedule_id: uuid.UUID, *, limit: int) -> list[ScheduleOccurrence]:
        rows = (
            (
                await self._session.execute(
                    select(SchedulerOccurrenceRecord)
                    .where(
                        SchedulerOccurrenceRecord.organization_id == self._org,
                        SchedulerOccurrenceRecord.schedule_id == schedule_id,
                    )
                    .order_by(SchedulerOccurrenceRecord.scheduled_for.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_occurrence(row) for row in rows]

    async def occurrence_row(
        self, occurrence_id: uuid.UUID, *, for_update: bool = False
    ) -> SchedulerOccurrenceRecord | None:
        query: Select[tuple[SchedulerOccurrenceRecord]] = select(SchedulerOccurrenceRecord).where(
            SchedulerOccurrenceRecord.organization_id == self._org,
            SchedulerOccurrenceRecord.id == occurrence_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def occurrence(self, occurrence_id: uuid.UUID) -> ScheduleOccurrence | None:
        row = await self.occurrence_row(occurrence_id)
        return None if row is None else _occurrence(row)

    async def claim_due(
        self, owner_id: uuid.UUID, *, correlation_id: str | None = None
    ) -> OccurrenceClaim | None:
        schedule_row = (
            await self._session.execute(
                select(SchedulerScheduleRecord)
                .join(
                    SchedulerOccurrenceRecord,
                    (
                        SchedulerOccurrenceRecord.organization_id
                        == SchedulerScheduleRecord.organization_id
                    )
                    & (SchedulerOccurrenceRecord.schedule_id == SchedulerScheduleRecord.id),
                )
                .where(
                    SchedulerScheduleRecord.organization_id == self._org,
                    SchedulerScheduleRecord.state == ScheduleState.ACTIVE.value,
                    SchedulerOccurrenceRecord.state == OccurrenceState.PENDING.value,
                    SchedulerOccurrenceRecord.scheduled_for <= func.clock_timestamp(),
                )
                .order_by(SchedulerOccurrenceRecord.scheduled_for, SchedulerOccurrenceRecord.id)
                .limit(1)
                .with_for_update(skip_locked=True, of=SchedulerScheduleRecord)
            )
        ).scalar_one_or_none()
        if schedule_row is None:
            return None
        row = (
            await self._session.execute(
                select(SchedulerOccurrenceRecord)
                .where(
                    SchedulerOccurrenceRecord.organization_id == self._org,
                    SchedulerOccurrenceRecord.schedule_id == schedule_row.id,
                    SchedulerOccurrenceRecord.state == OccurrenceState.PENDING.value,
                    SchedulerOccurrenceRecord.scheduled_for <= func.clock_timestamp(),
                )
                .order_by(SchedulerOccurrenceRecord.scheduled_for, SchedulerOccurrenceRecord.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        token = uuid.uuid4()
        require_occurrence_transition(OccurrenceState.PENDING, OccurrenceState.CLAIMED)
        row.state = OccurrenceState.CLAIMED.value
        row.claim_owner_id = owner_id
        row.claim_token = token
        row.claimed_at = await self.database_now()
        row.updated_at = row.claimed_at
        await self.add_transition(
            "OCCURRENCE",
            row.id,
            OccurrenceState.PENDING.value,
            OccurrenceState.CLAIMED.value,
            "OCCURRENCE_CLAIMED",
            "WORKER",
            correlation_id,
        )
        await self._session.flush()
        return OccurrenceClaim(
            occurrence=_occurrence(row),
            owner_id=owner_id,
            claim_token=token,
            workflow_input=dict(row.workflow_input),
        )

    async def begin_dispatch(self, claim: OccurrenceClaim) -> None:
        updated = (
            await self._session.execute(
                update(SchedulerOccurrenceRecord)
                .where(
                    SchedulerOccurrenceRecord.organization_id == self._org,
                    SchedulerOccurrenceRecord.id == claim.occurrence.id,
                    SchedulerOccurrenceRecord.state == OccurrenceState.CLAIMED.value,
                    SchedulerOccurrenceRecord.claim_owner_id == claim.owner_id,
                    SchedulerOccurrenceRecord.claim_token == claim.claim_token,
                )
                .values(
                    dispatch_attempt_count=SchedulerOccurrenceRecord.dispatch_attempt_count + 1,
                    dispatch_started_at=func.clock_timestamp(),
                    updated_at=func.clock_timestamp(),
                )
                .returning(SchedulerOccurrenceRecord.id)
            )
        ).scalar_one_or_none()
        if updated is None:
            raise ScheduleExecutionFencedError()

    async def finish_dispatch(
        self,
        claim: OccurrenceClaim,
        workflow_run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> ScheduleOccurrence:
        schedule = await self.schedule_row(claim.occurrence.schedule_id, for_update=True)
        if schedule is None:
            raise ScheduleExecutionFencedError()
        row = (
            await self._session.execute(
                update(SchedulerOccurrenceRecord)
                .where(
                    SchedulerOccurrenceRecord.organization_id == self._org,
                    SchedulerOccurrenceRecord.id == claim.occurrence.id,
                    SchedulerOccurrenceRecord.state == OccurrenceState.CLAIMED.value,
                    SchedulerOccurrenceRecord.claim_owner_id == claim.owner_id,
                    SchedulerOccurrenceRecord.claim_token == claim.claim_token,
                )
                .values(
                    state=OccurrenceState.DISPATCHED.value,
                    workflow_run_id=workflow_run_id,
                    updated_at=func.clock_timestamp(),
                )
                .returning(SchedulerOccurrenceRecord)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ScheduleExecutionFencedError()
        await self.add_transition(
            "OCCURRENCE",
            row.id,
            OccurrenceState.CLAIMED.value,
            OccurrenceState.DISPATCHED.value,
            "WORKFLOW_ACCEPTED",
            "WORKER",
            correlation_id,
        )
        if schedule.schedule_type == "ONE_TIME" and schedule.state == ScheduleState.ACTIVE.value:
            await self.transition_schedule(
                schedule,
                ScheduleState.COMPLETED,
                "ONE_TIME_DISPATCHED",
                source="WORKER",
                correlation_id=correlation_id,
            )
        return _occurrence(row)

    async def fail_dispatch(
        self, claim: OccurrenceClaim, error_code: str, *, correlation_id: str | None = None
    ) -> ScheduleOccurrence:
        schedule = await self.schedule_row(claim.occurrence.schedule_id, for_update=True)
        if schedule is None:
            raise ScheduleExecutionFencedError()
        row = (
            await self._session.execute(
                update(SchedulerOccurrenceRecord)
                .where(
                    SchedulerOccurrenceRecord.organization_id == self._org,
                    SchedulerOccurrenceRecord.id == claim.occurrence.id,
                    SchedulerOccurrenceRecord.state == OccurrenceState.CLAIMED.value,
                    SchedulerOccurrenceRecord.claim_owner_id == claim.owner_id,
                    SchedulerOccurrenceRecord.claim_token == claim.claim_token,
                )
                .values(
                    state=OccurrenceState.FAILED.value,
                    error_code=error_code,
                    updated_at=func.clock_timestamp(),
                )
                .returning(SchedulerOccurrenceRecord)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ScheduleExecutionFencedError()
        await self.add_transition(
            "OCCURRENCE",
            row.id,
            OccurrenceState.CLAIMED.value,
            OccurrenceState.FAILED.value,
            error_code,
            "WORKER",
            correlation_id,
        )
        if schedule.schedule_type == "ONE_TIME" and schedule.state == ScheduleState.ACTIVE.value:
            await self.transition_schedule(
                schedule,
                ScheduleState.COMPLETED,
                "ONE_TIME_DISPATCH_FAILED",
                source="WORKER",
                correlation_id=correlation_id,
            )
        return _occurrence(row)

    async def cancel_pending(
        self, schedule_id: uuid.UUID, *, correlation_id: str | None = None
    ) -> list[ScheduleOccurrence]:
        rows = (
            (
                await self._session.execute(
                    select(SchedulerOccurrenceRecord)
                    .where(
                        SchedulerOccurrenceRecord.organization_id == self._org,
                        SchedulerOccurrenceRecord.schedule_id == schedule_id,
                        SchedulerOccurrenceRecord.state == OccurrenceState.PENDING.value,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.state = OccurrenceState.CANCELLED.value
            row.updated_at = await self.database_now()
            await self.add_transition(
                "OCCURRENCE",
                row.id,
                OccurrenceState.PENDING.value,
                OccurrenceState.CANCELLED.value,
                "SCHEDULE_CANCELLED",
                "API",
                correlation_id,
            )
        return [_occurrence(row) for row in rows]

    async def cancel_occurrence(
        self, row: SchedulerOccurrenceRecord, *, correlation_id: str | None = None
    ) -> ScheduleOccurrence:
        require_occurrence_transition(OccurrenceState(row.state), OccurrenceState.CANCELLED)
        previous = row.state
        row.state = OccurrenceState.CANCELLED.value
        row.updated_at = await self.database_now()
        await self.add_transition(
            "OCCURRENCE",
            row.id,
            previous,
            OccurrenceState.CANCELLED.value,
            "OCCURRENCE_CANCELLED",
            "API",
            correlation_id,
        )
        await self._session.flush()
        return _occurrence(row)

    async def add_transition(
        self,
        entity_type: str,
        entity_id: uuid.UUID,
        from_state: str | None,
        to_state: str,
        reason_code: str,
        source: str,
        correlation_id: str | None = None,
    ) -> None:
        schedule_id = entity_id
        occurrence_id: uuid.UUID | None = None
        if entity_type == "OCCURRENCE":
            occurrence_id = entity_id
            schedule_id = (
                await self._session.execute(
                    select(SchedulerOccurrenceRecord.schedule_id).where(
                        SchedulerOccurrenceRecord.organization_id == self._org,
                        SchedulerOccurrenceRecord.id == entity_id,
                    )
                )
            ).scalar_one()
        self._session.add(
            SchedulerTransitionHistoryRecord(
                id=uuid.uuid7(),
                organization_id=self._org,
                schedule_id=schedule_id,
                occurrence_id=occurrence_id,
                entity_type=entity_type,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                reason_code=reason_code,
                source=source,
                correlation_id=correlation_id,
            )
        )

    async def transitions(self, entity_id: uuid.UUID, *, limit: int) -> list[ScheduleTransition]:
        rows = (
            (
                await self._session.execute(
                    select(SchedulerTransitionHistoryRecord)
                    .where(
                        SchedulerTransitionHistoryRecord.organization_id == self._org,
                        SchedulerTransitionHistoryRecord.entity_id == entity_id,
                    )
                    .order_by(SchedulerTransitionHistoryRecord.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            ScheduleTransition(
                id=row.id,
                organization_id=row.organization_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                from_state=row.from_state,
                to_state=row.to_state,
                reason_code=row.reason_code,
                source=row.source,
                correlation_id=row.correlation_id,
                created_at=row.created_at,
            )
            for row in rows
        ]
