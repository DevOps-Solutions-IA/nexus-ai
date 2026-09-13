"""PostgreSQL serialization and stale-worker fencing for NXS-P14."""

import asyncio
import uuid
from typing import Any

import pytest

from nexus_ai.workflows.entities import (
    AgentStepConfig,
    ConditionOperator,
    ConditionStepConfig,
    CreateWorkflowRequest,
    NoopStepConfig,
    StartWorkflowRunRequest,
    ToolStepConfig,
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


async def _run_steps(stack: Any, organization_id: Any, steps: tuple[WorkflowStepSpec, ...]) -> Any:
    definition = await stack.service.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"race.{uuid.uuid4().hex[:10]}",
            name="Race",
            steps=steps,
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


async def test_cancel_vs_claim_has_one_durable_order_and_fences_later_work(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _run(workflow_stack, organization.id)
    cancelled, claim = await asyncio.gather(
        workflow_stack.service.cancel_run(organization.id, run.id),
        workflow_stack.service.claim_next(organization.id, run.id),
    )
    assert cancelled.state is WorkflowRunState.CANCELLED
    assert (
        await workflow_stack.service.get_run(organization.id, run.id)
    ).state is WorkflowRunState.CANCELLED
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    if claim is not None:
        from nexus_ai.domain.workflows.repository import WorkflowRepository

        async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
            with pytest.raises(WorkflowExecutionFencedError):
                await WorkflowRepository(tenant).finish_step(claim, output={"late": True})


async def test_pause_vs_claim_has_one_durable_order_and_blocks_new_claims(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _run(workflow_stack, organization.id)
    paused, claim = await asyncio.gather(
        workflow_stack.service.pause_run(organization.id, run.id),
        workflow_stack.service.claim_next(organization.id, run.id),
    )
    assert paused.state is WorkflowRunState.PAUSED
    assert (
        await workflow_stack.service.get_run(organization.id, run.id)
    ).state is WorkflowRunState.PAUSED
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    steps = await workflow_stack.service.list_steps(organization.id, run.id)
    expected_state = "RUNNING" if claim is not None else "READY"
    assert steps[0].state.value == expected_state


async def test_duplicate_condition_completion_selects_one_branch_once(
    workflow_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.workflows.repository import WorkflowRepository

    organization = await make_organization()
    condition = WorkflowStepSpec(
        key="choose",
        step_type=WorkflowStepType.CONDITION,
        config=ConditionStepConfig(
            path="input.priority",
            operator=ConditionOperator.EQUALS,
            value="high",
            then_steps=("selected",),
            else_steps=("skipped",),
        ),
    )

    def dependent(key: str) -> WorkflowStepSpec:
        return WorkflowStepSpec(
            key=key,
            step_type=WorkflowStepType.NOOP,
            depends_on=("choose",),
            config=NoopStepConfig(output={}),
        )

    run = await _run_steps(
        workflow_stack, organization.id, (condition, dependent("selected"), dependent("skipped"))
    )
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None

    async def finish() -> Any:
        async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
            return await WorkflowRepository(tenant).finish_condition(
                claim, output={"selected": True}, skipped_step_keys=("skipped",)
            )

    outcomes = await asyncio.gather(finish(), finish(), return_exceptions=True)
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, WorkflowExecutionFencedError) for item in outcomes) == 1
    steps = {
        step.step_key: step
        for step in await workflow_stack.service.list_steps(organization.id, run.id)
    }
    assert steps["selected"].state.value == "READY"
    assert steps["skipped"].state.value == "SKIPPED"


async def test_parent_completion_makes_child_ready_exactly_once(
    workflow_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.workflows.repository import WorkflowRepository

    organization = await make_organization()
    run = await _run_steps(
        workflow_stack,
        organization.id,
        (
            WorkflowStepSpec(
                key="parent",
                step_type=WorkflowStepType.NOOP,
                config=NoopStepConfig(output={}),
            ),
            WorkflowStepSpec(
                key="child",
                step_type=WorkflowStepType.NOOP,
                depends_on=("parent",),
                config=NoopStepConfig(output={}),
            ),
        ),
    )
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None

    async def finish() -> Any:
        async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
            return await WorkflowRepository(tenant).finish_step(claim, output={"done": True})

    outcomes = await asyncio.gather(finish(), finish(), return_exceptions=True)
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    steps = {
        step.step_key: step
        for step in await workflow_stack.service.list_steps(organization.id, run.id)
    }
    assert steps["child"].state.value == "READY"
    transitions = await workflow_stack.service.transitions(organization.id, run.id, limit=100)
    assert (
        sum(
            transition.entity_id == steps["child"].id and transition.to_state == "READY"
            for transition in transitions
        )
        == 1
    )


async def test_duplicate_tool_execution_has_one_p08_dispatch_authority(
    workflow_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
) -> None:
    from tests.integration.test_agent_service import _register_tool

    organization = await make_organization()
    principal = await make_tool_principal(organization)
    await _register_tool(workflow_stack.agent_stack, organization.id, mock_http_server)
    hits = 0

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, str]]:
        nonlocal hits
        del method, path, headers, body
        hits += 1
        return 200, {"id": "one"}

    mock_http_server.set_handler(handler)
    run = await _run_steps(
        workflow_stack,
        organization.id,
        (
            WorkflowStepSpec(
                key="tool",
                step_type=WorkflowStepType.TOOL,
                config=ToolStepConfig(tool_key="crm.get", arguments={"path_params": {"id": "one"}}),
            ),
        ),
    )
    outcomes = await asyncio.gather(
        workflow_stack.service.execute_next(principal, run.id),
        workflow_stack.service.execute_next(principal, run.id),
    )
    assert sum(item is not None for item in outcomes) == 1
    assert hits == 1


async def test_duplicate_agent_execution_has_one_p13_dispatch_authority(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    from nexus_ai.agents.models.fake import FakeModelTurn
    from tests.integration.test_agent_service import _provision

    organization = await make_organization()
    principal = await make_tool_principal(organization)
    agent = await _provision(workflow_stack.agent_stack, organization.id)
    workflow_stack.agent_stack.script.append(FakeModelTurn(content="one"))
    run = await _run_steps(
        workflow_stack,
        organization.id,
        (
            WorkflowStepSpec(
                key="agent",
                step_type=WorkflowStepType.AGENT,
                config=AgentStepConfig(agent_id=agent.id, prompt="Decide"),
            ),
        ),
    )
    outcomes = await asyncio.gather(
        workflow_stack.service.execute_next(principal, run.id),
        workflow_stack.service.execute_next(principal, run.id),
    )
    assert sum(item is not None for item in outcomes) == 1
    assert len(workflow_stack.agent_stack.provider.calls) == 1
