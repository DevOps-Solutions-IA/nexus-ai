"""P15 crash evidence and ambiguous P14 dispatch behavior."""

import datetime as dt
import uuid
from typing import Any

import pytest

from nexus_ai.scheduler.entities import CreateScheduleRequest
from nexus_ai.scheduler.state_machine import OccurrenceState
from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    NoopStepConfig,
    WorkflowStepSpec,
    WorkflowStepType,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_worker_crash_after_claim_leaves_durable_p25_evidence(
    scheduler_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    definition = await scheduler_stack.workflows.create_definition(
        organization.id,
        CreateWorkflowRequest(
            workflow_key=f"scheduler.crash.{uuid.uuid4().hex[:8]}",
            name="Crash evidence",
            steps=(
                WorkflowStepSpec(
                    key="done", step_type=WorkflowStepType.NOOP, config=NoopStepConfig()
                ),
            ),
        ),
    )
    version = await scheduler_stack.workflows.publish(organization.id, definition.id)
    schedule = await scheduler_stack.service.create_schedule(
        organization.id,
        CreateScheduleRequest(
            schedule_key="crash.claim",
            workflow_version_id=version.id,
            schedule_type="ONE_TIME",
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        ),
    )
    await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    await scheduler_stack.service.materialize_due(organization.id)
    claim = await scheduler_stack.service.claim_due(organization.id, uuid.uuid4())
    assert claim is not None

    durable = await scheduler_stack.service.get_occurrence(organization.id, claim.occurrence.id)
    assert durable.state is OccurrenceState.CLAIMED
    assert durable.claim_owner_id == claim.owner_id
    assert durable.claim_token == claim.claim_token
    assert durable.claimed_at is not None
    assert durable.workflow_run_id is None
    assert await scheduler_stack.service.claim_due(organization.id, uuid.uuid4()) is None
