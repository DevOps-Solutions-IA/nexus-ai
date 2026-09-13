"""P14 crash evidence persists; P25 owns automatic reconciliation."""

import uuid
from typing import Any

import pytest

from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    NoopStepConfig,
    StartWorkflowRunRequest,
    WorkflowStepSpec,
    WorkflowStepType,
)
from nexus_ai.workflows.state_machine import WorkflowStepState

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_crash_after_claim_leaves_durable_ambiguous_evidence_without_reassignment(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    definition = await workflow_stack.service.create_definition(
        organization.id,
        CreateWorkflowRequest(
            workflow_key=f"crash.{uuid.uuid4().hex[:10]}",
            name="Crash boundary",
            steps=(
                WorkflowStepSpec(
                    key="effect",
                    step_type=WorkflowStepType.NOOP,
                    config=NoopStepConfig(output={"ok": True}),
                ),
            ),
        ),
    )
    version = await workflow_stack.service.publish(organization.id, definition.id)
    run = await workflow_stack.service.start_run(
        organization.id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )
    claim = await workflow_stack.service.claim_next(organization.id, run.id, owner_id=uuid.uuid4())
    assert claim is not None

    after_restart = await workflow_stack.service.list_steps(organization.id, run.id)
    assert after_restart[0].state is WorkflowStepState.RUNNING
    assert after_restart[0].claim_token == claim.claim_token
    assert after_restart[0].execution_owner_id == claim.owner_id
    assert after_restart[0].claimed_at is not None
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
