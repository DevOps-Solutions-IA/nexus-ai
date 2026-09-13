"""P15 durable Scheduler behavior against real PostgreSQL and P14."""

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from nexus_ai.domain.scheduler.models import SchedulerOccurrenceRecord
from nexus_ai.domain.scheduler.repository import SchedulerRepository
from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    MisfirePolicy,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduleType,
    UpdateScheduleRequest,
)
from nexus_ai.scheduler.errors import (
    ScheduleConflictError,
    ScheduleInvalidStateError,
    ScheduleNotFoundError,
)
from nexus_ai.scheduler.recurrence import TemporalSlot
from nexus_ai.scheduler.state_machine import OccurrenceState, ScheduleState
from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    NoopStepConfig,
    StartWorkflowRunRequest,
    WorkflowStepSpec,
    WorkflowStepType,
)
from nexus_ai.workflows.errors import WorkflowVersionNotFoundError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _version(stack: Any, organization_id: uuid.UUID) -> Any:
    definition = await stack.workflows.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"scheduled.{uuid.uuid4().hex[:10]}",
            name="Scheduled workflow",
            steps=(
                WorkflowStepSpec(
                    key="done",
                    step_type=WorkflowStepType.NOOP,
                    config=NoopStepConfig(output={"ok": True}),
                ),
            ),
        ),
    )
    return await stack.workflows.publish(organization_id, definition.id)


def _request(version_id: uuid.UUID, **overrides: object) -> CreateScheduleRequest:
    values: dict[str, object] = {
        "schedule_key": f"schedule.{uuid.uuid4().hex[:10]}",
        "workflow_version_id": version_id,
        "schedule_type": "ONE_TIME",
        "timezone": "UTC",
        "start_at": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        "input": {"source": "scheduler"},
    }
    values.update(overrides)
    return CreateScheduleRequest.model_validate(values)


async def test_one_time_materialize_claim_and_dispatches_only_through_p14(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await scheduler_stack.service.create_schedule(organization.id, _request(version.id))
    assert schedule.state is ScheduleState.DRAFT
    active = await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    assert active.state is ScheduleState.ACTIVE
    occurrences = await scheduler_stack.service.materialize_due(organization.id)
    assert len(occurrences) == 1
    assert occurrences[0].state is OccurrenceState.PENDING
    assert (
        await scheduler_stack.service.get_schedule(organization.id, schedule.id)
    ).state is ScheduleState.ACTIVE
    claim = await scheduler_stack.service.claim_due(organization.id, uuid.uuid4())
    assert claim is not None and claim.occurrence.id == occurrences[0].id
    dispatched = await scheduler_stack.service.dispatch_claim(organization.id, claim)
    assert dispatched.state is OccurrenceState.DISPATCHED
    run = await scheduler_stack.workflows.get_run(organization.id, dispatched.workflow_run_id)
    assert run.id == dispatched.workflow_run_id
    assert run.idempotency_key == dispatched.p14_idempotency_key
    assert (
        await scheduler_stack.service.get_schedule(organization.id, schedule.id)
    ).state is ScheduleState.COMPLETED


async def test_ambiguous_p14_success_replay_returns_same_logical_run(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await scheduler_stack.service.create_schedule(
        organization.id,
        _request(
            version.id,
            schedule_type=ScheduleType.RECURRING,
            recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY),
        ),
    )
    await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    await scheduler_stack.service.materialize_due(organization.id)
    claim = await scheduler_stack.service.claim_due(organization.id, uuid.uuid4())
    assert claim is not None
    first = await scheduler_stack.workflows.start_run(
        organization.id,
        StartWorkflowRunRequest(
            workflow_version_id=version.id,
            input=claim.workflow_input,
            idempotency_key=claim.occurrence.p14_idempotency_key,
        ),
    )
    completed = await scheduler_stack.service.dispatch_claim(organization.id, claim)
    assert completed.workflow_run_id == first.id
    runs = await scheduler_stack.workflows.list_runs(organization.id, limit=10)
    assert [run.id for run in runs].count(first.id) == 1


async def test_concurrent_materialization_and_claim_have_single_authority(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await scheduler_stack.service.create_schedule(
        organization.id,
        _request(
            version.id,
            schedule_type=ScheduleType.RECURRING,
            recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY),
        ),
    )
    active = await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    first, second = await asyncio.gather(
        scheduler_stack.service.materialize_due(organization.id),
        scheduler_stack.service.materialize_due(organization.id),
    )
    assert len(first) + len(second) == 1
    stored = await scheduler_stack.service.list_occurrences(organization.id, schedule.id, limit=10)
    assert len(stored) == 1
    assert stored[0].schedule_revision == active.revision
    claims = await asyncio.gather(
        scheduler_stack.service.claim_due(organization.id, uuid.uuid4()),
        scheduler_stack.service.claim_due(organization.id, uuid.uuid4()),
    )
    assert len([claim for claim in claims if claim is not None]) == 1


async def test_same_revision_same_slot_is_rejected_by_database_uniqueness(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await scheduler_stack.service.create_schedule(organization.id, _request(version.id))
    await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    first = (await scheduler_stack.service.materialize_due(organization.id))[0]
    slot = TemporalSlot(
        intended_local_time=first.intended_local_time,
        scheduled_for=first.scheduled_for,
        utc_offset_seconds=first.utc_offset_seconds,
        fold=first.fold,
    )

    with pytest.raises(IntegrityError):
        async with scheduler_stack.database.tenant_transaction(organization.id) as tenant:
            repo = SchedulerRepository(tenant)
            row = await repo.schedule_row(schedule.id, for_update=True)
            assert row is not None
            await repo.add_occurrence(
                row,
                slot,
                state=OccurrenceState.PENDING,
                reason="OCCURRENCE_CREATED",
            )


async def test_pause_edit_resume_can_rematerialize_same_slot_under_new_revision(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    now = dt.datetime.now(dt.UTC)
    local_time = (now - dt.timedelta(minutes=2)).time().replace(second=0, microsecond=0)
    schedule = await scheduler_stack.service.create_schedule(
        organization.id,
        _request(
            version.id,
            schedule_type=ScheduleType.RECURRING,
            start_at=now - dt.timedelta(days=1),
            recurrence=RecurrenceSpec(
                frequency=RecurrenceFrequency.DAILY,
                local_time=local_time,
            ),
            misfire_policy=MisfirePolicy.FIRE_ONCE,
        ),
    )
    active = await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    first = await scheduler_stack.service.materialize_due(organization.id)
    assert len(first) == 1
    assert first[0].schedule_revision == active.revision

    paused = await scheduler_stack.service.pause_schedule(organization.id, schedule.id)
    edited = await scheduler_stack.service.update_schedule(
        organization.id,
        schedule.id,
        UpdateScheduleRequest(expected_revision=paused.revision, input={"revision": "new"}),
    )
    assert edited.revision == paused.revision + 1
    resumed = await scheduler_stack.service.resume_schedule(organization.id, schedule.id)
    second = await scheduler_stack.service.materialize_due(organization.id)

    assert len(second) == 1
    assert second[0].schedule_revision == resumed.revision
    assert second[0].intended_local_time == first[0].intended_local_time
    assert second[0].occurrence_key != first[0].occurrence_key
    assert second[0].p14_idempotency_key != first[0].p14_idempotency_key
    async with scheduler_stack.database.tenant_transaction(organization.id) as tenant:
        rows = (
            (
                await tenant.session.execute(
                    select(SchedulerOccurrenceRecord)
                    .where(SchedulerOccurrenceRecord.schedule_id == schedule.id)
                    .order_by(SchedulerOccurrenceRecord.schedule_revision)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert rows[0].schedule_revision == active.revision
    assert rows[0].workflow_input == {"source": "scheduler"}
    assert rows[0].workflow_version_id == version.id
    assert rows[0].timezone == "UTC"
    assert rows[0].misfire_policy == MisfirePolicy.FIRE_ONCE.value
    assert rows[1].schedule_revision == resumed.revision
    assert rows[1].workflow_input == {"revision": "new"}


async def test_misfire_policies_are_bounded(scheduler_stack: Any, make_organization: Any) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    recurrence = RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY)
    for policy, maximum, expected_state in (
        (MisfirePolicy.SKIP, 1, OccurrenceState.SKIPPED),
        (MisfirePolicy.FIRE_ONCE, 1, OccurrenceState.PENDING),
        (MisfirePolicy.CATCH_UP_BOUNDED, 3, OccurrenceState.PENDING),
    ):
        schedule = await scheduler_stack.service.create_schedule(
            organization.id,
            _request(
                version.id,
                schedule_key=f"misfire.{policy.value.lower()}",
                start_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
                schedule_type=ScheduleType.RECURRING,
                recurrence=recurrence,
                misfire_policy=policy,
                max_catch_up=maximum,
            ),
        )
        await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
        rows = await scheduler_stack.service.materialize_due(organization.id, limit=10)
        own = [row for row in rows if row.schedule_id == schedule.id]
        assert len(own) <= maximum
        assert own and all(row.state is expected_state for row in own)


async def test_pause_cancel_revision_and_terminal_absorption(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await scheduler_stack.service.create_schedule(organization.id, _request(version.id))
    updated = await scheduler_stack.service.update_schedule(
        organization.id,
        schedule.id,
        UpdateScheduleRequest(expected_revision=1, input={"revision": 2}),
    )
    assert updated.revision == 2
    with pytest.raises(ScheduleConflictError):
        await scheduler_stack.service.update_schedule(
            organization.id, schedule.id, UpdateScheduleRequest(expected_revision=1)
        )
    await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    paused = await scheduler_stack.service.pause_schedule(organization.id, schedule.id)
    assert paused.state is ScheduleState.PAUSED
    resumed = await scheduler_stack.service.resume_schedule(organization.id, schedule.id)
    assert resumed.state is ScheduleState.ACTIVE
    cancelled = await scheduler_stack.service.cancel_schedule(organization.id, schedule.id)
    assert cancelled.state is ScheduleState.CANCELLED
    with pytest.raises(ScheduleInvalidStateError):
        await scheduler_stack.service.resume_schedule(organization.id, schedule.id)


async def test_tenant_isolation_and_forced_rls(
    scheduler_stack: Any, make_organization: Any
) -> None:
    first, second = await make_organization(), await make_organization()
    version = await _version(scheduler_stack, first.id)
    schedule = await scheduler_stack.service.create_schedule(first.id, _request(version.id))
    with pytest.raises(ScheduleNotFoundError):
        await scheduler_stack.service.get_schedule(second.id, schedule.id)
    with pytest.raises(WorkflowVersionNotFoundError):
        await scheduler_stack.service.create_schedule(second.id, _request(version.id))
    async with scheduler_stack.database.transaction() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname LIKE 'scheduler_%' AND relkind='r' ORDER BY relname"
                )
            )
        ).all()
    assert len(rows) == 3
    assert all(row[1] and row[2] for row in rows)
