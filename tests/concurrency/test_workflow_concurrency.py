"""PostgreSQL serialization and stale-worker fencing for NXS-P14."""

import asyncio
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
from nexus_ai.workflows.errors import WorkflowExecutionFencedError, WorkflowInvalidStateError
from nexus_ai.workflows.state_machine import WorkflowRunState

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _run(stack: Any, organization_id: Any) -> Any:
    definition = await stack.service.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"race.{uuid.uuid4().hex[:10]}",
            name="Race",
            steps=(
                WorkflowStepSpec(
                    key="effect",
                    step_type=WorkflowStepType.NOOP,
                    config=NoopStepConfig(output={"ok": True}),
                ),
            ),
        ),
    )
    version = await stack.service.publish(organization_id, definition.id)
    return await stack.service.start_run(
        organization_id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )


async def test_two_workers_claim_once_and_stale_completion_is_fenced(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _run(workflow_stack, organization.id)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    first, second = await asyncio.gather(
        workflow_stack.service.claim_next(organization.id, run.id, owner_id=owner_a),
        workflow_stack.service.claim_next(organization.id, run.id, owner_id=owner_b),
    )
    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    claim = claims[0]
    stale = claim.model_copy(update={"claim_token": uuid.uuid4()})
    from nexus_ai.domain.workflows.repository import WorkflowRepository

    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(WorkflowExecutionFencedError):
            await WorkflowRepository(tenant).finish_step(stale, output={"bad": True})


async def test_cancel_fence_beats_stale_claim_completion(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _run(workflow_stack, organization.id)
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None
    cancelled = await workflow_stack.service.cancel_run(organization.id, run.id)
    assert cancelled.state is WorkflowRunState.CANCELLED
    from nexus_ai.domain.workflows.repository import WorkflowRepository

    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(WorkflowExecutionFencedError):
            await WorkflowRepository(tenant).finish_step(claim, output={"late": True})


async def test_resume_cancel_race_never_resurrects_cancelled(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _run(workflow_stack, organization.id)
    await workflow_stack.service.pause_run(organization.id, run.id)
    results = await asyncio.gather(
        workflow_stack.service.resume_run(organization.id, run.id),
        workflow_stack.service.cancel_run(organization.id, run.id),
        return_exceptions=True,
    )
    final = await workflow_stack.service.get_run(organization.id, run.id)
    assert final.state in {WorkflowRunState.RUNNING, WorkflowRunState.CANCELLED}
    if final.state is WorkflowRunState.CANCELLED:
        with pytest.raises(WorkflowInvalidStateError):
            await workflow_stack.service.resume_run(organization.id, run.id)
    assert any(not isinstance(item, BaseException) for item in results)


async def test_two_workers_start_same_semantic_key_once(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    definition = await workflow_stack.service.create_definition(
        organization.id,
        CreateWorkflowRequest(
            workflow_key=f"start.{uuid.uuid4().hex[:10]}",
            name="Start race",
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
    request = StartWorkflowRunRequest(
        workflow_version_id=version.id,
        input={"same": True},
        idempotency_key="concurrent-start-key",
    )
    first, second = await asyncio.gather(
        workflow_stack.service.start_run(organization.id, request),
        workflow_stack.service.start_run(organization.id, request),
    )
    assert first.id == second.id
    assert len(await workflow_stack.service.list_runs(organization.id, limit=10)) == 1
