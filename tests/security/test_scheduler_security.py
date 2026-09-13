"""P15 attack-surface and tenant-boundary security assertions."""

import datetime as dt
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.domain.scheduler.repository import SchedulerRepository
from nexus_ai.scheduler.entities import CreateScheduleRequest, RecurrenceSpec, UpdateScheduleRequest
from nexus_ai.scheduler.errors import OccurrenceNotFoundError, ScheduleNotFoundError
from tests.integration.test_scheduler_service import _request, _version

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_type", "SHELL"),
        ("target_type", "SQL"),
        ("target_type", "ARBITRARY_HTTP"),
        ("target_type", "PYTHON"),
        ("target_type", "TOOL_DIRECT"),
        ("target_type", "AGENT_DIRECT"),
        ("provider", "openai"),
        ("authorization", "Bearer secret"),
    ],
)
def test_scheduler_has_no_direct_execution_surface(field: str, value: str) -> None:
    payload = {
        "schedule_key": "security.schedule",
        "workflow_version_id": str(uuid4()),
        "schedule_type": "ONE_TIME",
        "timezone": "UTC",
        "start_at": dt.datetime.now(dt.UTC).isoformat(),
        field: value,
    }
    with pytest.raises(ValidationError):
        CreateScheduleRequest.model_validate(payload)


def test_recurrence_rejects_unbounded_or_expression_input() -> None:
    with pytest.raises(ValidationError):
        RecurrenceSpec.model_validate({"frequency": "MINUTELY", "interval": 10001})
    with pytest.raises(ValidationError):
        RecurrenceSpec.model_validate(
            {"frequency": "MINUTELY", "cron": "* * * * *", "expression": "__import__('os')"}
        )


def test_oversized_workflow_input_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateScheduleRequest(
            schedule_key="security.large",
            workflow_version_id=uuid4(),
            schedule_type="ONE_TIME",
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC),
            input={"payload": "x" * 70_000},
        )


async def test_cross_tenant_schedule_and_occurrence_operations_fail_closed(
    scheduler_stack: Any, make_organization: Any
) -> None:
    first = await make_organization()
    second = await make_organization()
    version = await _version(scheduler_stack, first.id)
    schedule = await scheduler_stack.service.create_schedule(first.id, _request(version.id))
    await scheduler_stack.service.activate_schedule(first.id, schedule.id)
    occurrences = await scheduler_stack.service.materialize_due(first.id)
    occurrence = occurrences[0]

    with pytest.raises(ScheduleNotFoundError):
        await scheduler_stack.service.get_schedule(second.id, schedule.id)
    with pytest.raises(ScheduleNotFoundError):
        await scheduler_stack.service.update_schedule(
            second.id, schedule.id, UpdateScheduleRequest(expected_revision=1)
        )
    with pytest.raises(ScheduleNotFoundError):
        await scheduler_stack.service.activate_schedule(second.id, schedule.id)
    with pytest.raises(ScheduleNotFoundError):
        await scheduler_stack.service.cancel_schedule(second.id, schedule.id)
    with pytest.raises(ScheduleNotFoundError):
        await scheduler_stack.service.list_occurrences(second.id, schedule.id, limit=10)
    with pytest.raises(OccurrenceNotFoundError):
        await scheduler_stack.service.get_occurrence(second.id, occurrence.id)

    async with scheduler_stack.database.tenant_transaction(second.id) as tenant:
        repository = SchedulerRepository(tenant)
        assert await repository.schedule(schedule.id) is None
        assert await repository.occurrence(occurrence.id) is None
        visible = (
            await tenant.session.execute(
                text("SELECT count(*) FROM scheduler_schedules WHERE id = :id"),
                {"id": schedule.id},
            )
        ).scalar_one()
        assert visible == 0


async def test_cross_tenant_workflow_version_fk_is_a_database_backstop(
    scheduler_stack: Any, make_organization: Any
) -> None:
    first = await make_organization()
    second = await make_organization()
    version = await _version(scheduler_stack, first.id)
    with pytest.raises(DBAPIError):
        async with scheduler_stack.database.tenant_transaction(second.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO scheduler_schedules "
                    "(id, organization_id, schedule_key, target_type, workflow_version_id, "
                    "schedule_type, timezone, input_payload, start_at, state, misfire_policy, "
                    "max_catch_up, revision, timezone_data_version) VALUES "
                    "(:id, :org, 'forged.cross-tenant', 'START_WORKFLOW', :version, "
                    "'ONE_TIME', 'UTC', '{}'::jsonb, now(), 'DRAFT', 'FIRE_ONCE', 1, 1, "
                    "'2026.3')"
                ),
                {"id": uuid4(), "org": second.id, "version": version.id},
            )
