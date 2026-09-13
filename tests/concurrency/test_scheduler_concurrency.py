"""PostgreSQL multi-worker fencing tests for NXS-P15."""

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest

from nexus_ai.domain.scheduler.repository import SchedulerRepository
from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduleType,
    UpdateScheduleRequest,
)
from nexus_ai.scheduler.errors import (
    ScheduleConflictError,
    ScheduleExecutionFencedError,
    ScheduleInvalidStateError,
)
from nexus_ai.scheduler.state_machine import OccurrenceState, ScheduleState
from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    NoopStepConfig,
    WorkflowStepSpec,
    WorkflowStepType,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _active_due(stack: Any, organization_id: uuid.UUID) -> Any:
    definition = await stack.workflows.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"scheduler.race.{uuid.uuid4().hex[:8]}",
            name="Scheduler race",
            steps=(
                WorkflowStepSpec(
                    key="done", step_type=WorkflowStepType.NOOP, config=NoopStepConfig()
                ),
            ),
        ),
    )
    version = await stack.workflows.publish(organization_id, definition.id)
    schedule = await stack.service.create_schedule(
        organization_id,
        CreateScheduleRequest(
            schedule_key=f"race.{uuid.uuid4().hex[:8]}",
            workflow_version_id=version.id,
            schedule_type=ScheduleType.RECURRING,
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2),
            recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY),
        ),
    )
    await stack.service.activate_schedule(organization_id, schedule.id)
    return schedule


async def _due(stack: Any, organization_id: uuid.UUID) -> Any:
    schedule = await _active_due(stack, organization_id)
    await stack.service.materialize_due(organization_id)
    return schedule


async def test_two_workers_claim_once_and_stale_owner_is_fenced(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    await _due(scheduler_stack, organization.id)
    first, second = await asyncio.gather(
        scheduler_stack.service.claim_due(organization.id, uuid.uuid4()),
        scheduler_stack.service.claim_due(organization.id, uuid.uuid4()),
    )
    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    stale = claims[0].model_copy(update={"claim_token": uuid.uuid4()})
    async with scheduler_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(ScheduleExecutionFencedError):
            await SchedulerRepository(tenant).begin_dispatch(stale)


async def test_cancel_vs_claim_has_one_serial_order(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    schedule = await _due(scheduler_stack, organization.id)
    cancelled, claim = await asyncio.gather(
        scheduler_stack.service.cancel_schedule(organization.id, schedule.id),
        scheduler_stack.service.claim_due(organization.id, uuid.uuid4()),
    )
    assert cancelled.state.value == "CANCELLED"
    assert await scheduler_stack.service.claim_due(organization.id, uuid.uuid4()) is None
    if claim is not None:
        assert claim.occurrence.claim_token is not None


async def test_claim_before_pause_or_cancel_retains_dispatch_authority(
    scheduler_stack: Any, make_organization: Any
) -> None:
    for transition in ("pause", "cancel"):
        organization = await make_organization()
        schedule = await _due(scheduler_stack, organization.id)
        claim = await scheduler_stack.service.claim_due(organization.id, uuid.uuid4())
        assert claim is not None
        if transition == "pause":
            await scheduler_stack.service.pause_schedule(organization.id, schedule.id)
            expected = ScheduleState.PAUSED
        else:
            await scheduler_stack.service.cancel_schedule(organization.id, schedule.id)
            expected = ScheduleState.CANCELLED
        dispatched = await scheduler_stack.service.dispatch_claim(organization.id, claim)
        assert dispatched.state is OccurrenceState.DISPATCHED
        assert (
            await scheduler_stack.service.get_schedule(organization.id, schedule.id)
        ).state is expected


async def test_edit_vs_materialize_preserves_one_immutable_revision(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    schedule = await _active_due(scheduler_stack, organization.id)
    materialized, edit = await asyncio.gather(
        scheduler_stack.service.materialize_due(organization.id),
        scheduler_stack.service.update_schedule(
            organization.id,
            schedule.id,
            UpdateScheduleRequest(expected_revision=2, input={"changed": True}),
        ),
        return_exceptions=True,
    )
    assert isinstance(edit, ScheduleInvalidStateError)
    occurrences = await scheduler_stack.service.list_occurrences(
        organization.id, schedule.id, limit=10
    )
    assert len(occurrences) == 1
    assert occurrences[0].schedule_revision == 2
    assert len(materialized) == 1


async def test_pause_vs_claim_and_resume_vs_tick_serialize_on_schedule(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    schedule = await _due(scheduler_stack, organization.id)
    paused, claim = await asyncio.gather(
        scheduler_stack.service.pause_schedule(organization.id, schedule.id),
        scheduler_stack.service.claim_due(organization.id, uuid.uuid4()),
    )
    assert paused.state is ScheduleState.PAUSED
    assert await scheduler_stack.service.claim_due(organization.id, uuid.uuid4()) is None
    if claim is not None:
        assert claim.occurrence.claim_token is not None

    other = await _active_due(scheduler_stack, organization.id)
    await scheduler_stack.service.pause_schedule(organization.id, other.id)
    resumed, tick = await asyncio.gather(
        scheduler_stack.service.resume_schedule(organization.id, other.id),
        scheduler_stack.service.materialize_due(organization.id),
    )
    assert resumed.state is ScheduleState.ACTIVE
    await scheduler_stack.service.materialize_due(organization.id)
    occurrences = await scheduler_stack.service.list_occurrences(
        organization.id, other.id, limit=10
    )
    assert len(occurrences) == 1
    assert len(tick) <= 1


async def test_concurrent_same_schedule_key_has_one_durable_definition(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    definition = await scheduler_stack.workflows.create_definition(
        organization.id,
        CreateWorkflowRequest(
            workflow_key=f"scheduler.create.{uuid.uuid4().hex[:8]}",
            name="Create race",
            steps=(
                WorkflowStepSpec(
                    key="done", step_type=WorkflowStepType.NOOP, config=NoopStepConfig()
                ),
            ),
        ),
    )
    version = await scheduler_stack.workflows.publish(organization.id, definition.id)
    request = CreateScheduleRequest(
        schedule_key="same.schedule.key",
        workflow_version_id=version.id,
        schedule_type="ONE_TIME",
        timezone="UTC",
        start_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1),
    )
    results = await asyncio.gather(
        scheduler_stack.service.create_schedule(organization.id, request),
        scheduler_stack.service.create_schedule(organization.id, request),
        return_exceptions=True,
    )
    assert len([item for item in results if not isinstance(item, BaseException)]) == 1
    assert len([item for item in results if isinstance(item, ScheduleConflictError)]) == 1
