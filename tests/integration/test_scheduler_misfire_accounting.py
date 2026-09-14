"""Durable, bounded misfire accounting for NXS-P15 corrective #2."""

import asyncio
import datetime as dt
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from nexus_ai.domain.scheduler.models import SchedulerTransitionHistoryRecord
from nexus_ai.domain.scheduler.repository import SchedulerRepository
from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    MisfirePolicy,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduleType,
    UpdateScheduleRequest,
)
from nexus_ai.scheduler.state_machine import OccurrenceState
from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    NoopStepConfig,
    WorkflowStepSpec,
    WorkflowStepType,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _version(stack: Any, organization_id: uuid.UUID) -> Any:
    definition = await stack.workflows.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"scheduler.misfire.{uuid.uuid4().hex[:10]}",
            name="Misfire accounting workflow",
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


def _request(
    workflow_version_id: uuid.UUID,
    *,
    policy: MisfirePolicy,
    start_at: dt.datetime,
    maximum: int = 1,
    schedule_type: ScheduleType = ScheduleType.RECURRING,
    timezone: str = "UTC",
    recurrence: RecurrenceSpec | None = None,
    end_at: dt.datetime | None = None,
) -> CreateScheduleRequest:
    return CreateScheduleRequest(
        schedule_key=f"misfire.accounting.{uuid.uuid4().hex[:10]}",
        workflow_version_id=workflow_version_id,
        schedule_type=schedule_type,
        timezone=timezone,
        start_at=start_at,
        end_at=end_at,
        recurrence=(
            recurrence
            if recurrence is not None or schedule_type is ScheduleType.ONE_TIME
            else RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY)
        ),
        misfire_policy=policy,
        max_catch_up=maximum,
    )


async def _accounting_rows(
    stack: Any, organization_id: uuid.UUID, schedule_id: uuid.UUID
) -> list[SchedulerTransitionHistoryRecord]:
    async with stack.database.tenant_transaction(organization_id) as tenant:
        return list(
            (
                await tenant.session.execute(
                    select(SchedulerTransitionHistoryRecord)
                    .where(
                        SchedulerTransitionHistoryRecord.schedule_id == schedule_id,
                        SchedulerTransitionHistoryRecord.reason_code.like("MISFIRE_%"),
                        SchedulerTransitionHistoryRecord.detail.is_not(None),
                    )
                    .order_by(SchedulerTransitionHistoryRecord.created_at)
                )
            )
            .scalars()
            .all()
        )


def _detail(row: SchedulerTransitionHistoryRecord) -> dict[str, Any]:
    return json.loads(row.detail or "{}")


async def _active(
    stack: Any,
    organization_id: uuid.UUID,
    request: CreateScheduleRequest,
) -> Any:
    schedule = await stack.service.create_schedule(organization_id, request)
    return await stack.service.activate_schedule(organization_id, schedule.id)


async def test_skip_single_elapsed_slot_is_durably_skipped_without_summary(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.SKIP,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
            schedule_type=ScheduleType.ONE_TIME,
            recurrence=None,
        ),
    )

    rows = await scheduler_stack.service.materialize_due(organization.id)

    assert len(rows) == 1
    assert rows[0].state is OccurrenceState.SKIPPED
    assert rows[0].error_code == "MISFIRE_SKIPPED"
    assert await _accounting_rows(scheduler_stack, organization.id, schedule.id) == []


async def test_skip_many_materializes_bounded_latest_slots_and_summarizes_older(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.SKIP,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=12),
        ),
    )

    rows = await scheduler_stack.service.materialize_due(organization.id, limit=3)
    accounting = await _accounting_rows(scheduler_stack, organization.id, schedule.id)

    assert len(rows) == 3
    assert all(row.state is OccurrenceState.SKIPPED for row in rows)
    assert len(accounting) == 1
    detail = _detail(accounting[0])
    assert detail["disposition"] == "SKIPPED"
    assert detail["skipped_count"] >= 9
    assert detail["coalesced_count"] == 0
    assert max(row.intended_local_time for row in rows) > dt.datetime.fromisoformat(
        detail["last_omitted_local_time"]
    )


async def test_fire_once_materializes_latest_and_accounts_for_earlier_slots(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.FIRE_ONCE,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=12),
        ),
    )

    rows = await scheduler_stack.service.materialize_due(organization.id)
    accounting = await _accounting_rows(scheduler_stack, organization.id, schedule.id)

    assert len(rows) == 1
    assert rows[0].state is OccurrenceState.PENDING
    assert len(accounting) == 1
    detail = _detail(accounting[0])
    assert detail["coalesced_count"] >= 10
    assert detail["policy"] == MisfirePolicy.FIRE_ONCE.value
    assert rows[0].intended_local_time > dt.datetime.fromisoformat(
        detail["last_omitted_local_time"]
    )


async def test_catch_up_below_bound_materializes_every_elapsed_slot(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.CATCH_UP_BOUNDED,
            maximum=10,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2),
        ),
    )

    rows = await scheduler_stack.service.materialize_due(organization.id)

    assert 2 <= len(rows) <= 4
    assert all(row.state is OccurrenceState.PENDING for row in rows)
    assert await _accounting_rows(scheduler_stack, organization.id, schedule.id) == []


async def test_catch_up_above_bound_accounts_for_excess_slots(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.CATCH_UP_BOUNDED,
            maximum=3,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=12),
        ),
    )

    rows = await scheduler_stack.service.materialize_due(organization.id)
    accounting = await _accounting_rows(scheduler_stack, organization.id, schedule.id)

    assert len(rows) == 3
    assert all(row.state is OccurrenceState.PENDING for row in rows)
    assert len(accounting) == 1
    detail = _detail(accounting[0])
    assert detail["coalesced_count"] >= 9
    assert detail["policy"] == MisfirePolicy.CATCH_UP_BOUNDED.value


async def test_replay_does_not_duplicate_occurrences_or_accounting(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.FIRE_ONCE,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
        ),
    )

    first = await scheduler_stack.service.materialize_due(organization.id)
    second = await scheduler_stack.service.materialize_due(organization.id)

    assert len(first) == 1
    assert second == []
    assert len(await _accounting_rows(scheduler_stack, organization.id, schedule.id)) == 1
    stored = await scheduler_stack.service.list_occurrences(organization.id, schedule.id, limit=10)
    assert len(stored) == 1


async def test_two_workers_persist_one_occurrence_and_one_accounting_record(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.FIRE_ONCE,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
        ),
    )

    first, second = await asyncio.gather(
        scheduler_stack.service.materialize_due(organization.id),
        scheduler_stack.service.materialize_due(organization.id),
    )

    assert len(first) + len(second) == 1
    assert len(await _accounting_rows(scheduler_stack, organization.id, schedule.id)) == 1


async def test_accounting_failure_rolls_back_occurrence_and_cursor(
    scheduler_stack: Any, make_organization: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.FIRE_ONCE,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
        ),
    )
    original_cursor = schedule.next_fire_at
    original = SchedulerRepository.record_misfire_accounting

    async def fail_accounting(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("simulated accounting persistence failure")

    monkeypatch.setattr(SchedulerRepository, "record_misfire_accounting", fail_accounting)
    with pytest.raises(RuntimeError, match="simulated accounting persistence failure"):
        await scheduler_stack.service.materialize_due(organization.id)

    after_failure = await scheduler_stack.service.get_schedule(organization.id, schedule.id)
    assert after_failure.next_fire_at == original_cursor
    assert (
        await scheduler_stack.service.list_occurrences(organization.id, schedule.id, limit=10) == []
    )
    assert await _accounting_rows(scheduler_stack, organization.id, schedule.id) == []

    monkeypatch.setattr(SchedulerRepository, "record_misfire_accounting", original)
    assert len(await scheduler_stack.service.materialize_due(organization.id)) == 1
    assert len(await _accounting_rows(scheduler_stack, organization.id, schedule.id)) == 1


async def test_dst_gap_and_misfire_slots_are_both_durably_accounted(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.SKIP,
            start_at=dt.datetime(2026, 3, 8, 5, 0, tzinfo=dt.UTC),
            end_at=dt.datetime(2026, 3, 9, 8, 0, tzinfo=dt.UTC),
            timezone="America/New_York",
            recurrence=RecurrenceSpec(
                frequency=RecurrenceFrequency.DAILY,
                local_time=dt.time(2, 30),
            ),
        ),
    )

    rows = await scheduler_stack.service.materialize_due(organization.id, limit=10)

    assert len(rows) == 2
    assert {row.error_code for row in rows} == {
        "DST_NONEXISTENT_LOCAL_TIME",
        "MISFIRE_SKIPPED",
    }
    assert await _accounting_rows(scheduler_stack, organization.id, schedule.id) == []


async def test_revision_change_creates_distinct_accounting_identity_for_same_window(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=MisfirePolicy.FIRE_ONCE,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
        ),
    )
    await scheduler_stack.service.materialize_due(organization.id)
    first_accounting = await _accounting_rows(scheduler_stack, organization.id, schedule.id)
    paused = await scheduler_stack.service.pause_schedule(organization.id, schedule.id)
    edited = await scheduler_stack.service.update_schedule(
        organization.id,
        schedule.id,
        UpdateScheduleRequest(expected_revision=paused.revision, input={"revision": "new"}),
    )
    await scheduler_stack.service.resume_schedule(organization.id, schedule.id)

    await scheduler_stack.service.materialize_due(organization.id)
    accounting = await _accounting_rows(scheduler_stack, organization.id, schedule.id)

    assert edited.revision == paused.revision + 1
    assert len(first_accounting) == 1
    assert len(accounting) == 2
    details = [_detail(row) for row in accounting]
    assert details[0]["schedule_revision"] != details[1]["schedule_revision"]
    assert details[0]["accounting_key"] != details[1]["accounting_key"]


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (MisfirePolicy.SKIP, "MISFIRE_SKIPPED"),
        (MisfirePolicy.FIRE_ONCE, "MISFIRE_FIRE_ONCE_COALESCED"),
        (MisfirePolicy.CATCH_UP_BOUNDED, "MISFIRE_CATCH_UP_COALESCED"),
    ],
)
async def test_accounting_identity_is_stable_for_same_window(
    scheduler_stack: Any,
    make_organization: Any,
    policy: MisfirePolicy,
    reason: str,
) -> None:
    organization = await make_organization()
    version = await _version(scheduler_stack, organization.id)
    schedule = await _active(
        scheduler_stack,
        organization.id,
        _request(
            version.id,
            policy=policy,
            maximum=2 if policy is MisfirePolicy.CATCH_UP_BOUNDED else 1,
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
        ),
    )
    await scheduler_stack.service.materialize_due(organization.id, limit=2)
    accounting = await _accounting_rows(scheduler_stack, organization.id, schedule.id)
    assert len(accounting) == 1
    assert accounting[0].reason_code == reason
    key = _detail(accounting[0])["accounting_key"]
    assert key.startswith(f"v1:r{schedule.revision}:")
    assert len(key) < 100
