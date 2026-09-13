"""P14 durable service behavior against real PostgreSQL and P04 outbox."""

from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.workflows.entities import (
    AgentStepConfig,
    ConditionOperator,
    ConditionStepConfig,
    CreateWorkflowRequest,
    NoopStepConfig,
    StartWorkflowRunRequest,
    ToolStepConfig,
    UpdateWorkflowRequest,
    WorkflowStepSpec,
    WorkflowStepType,
)
from nexus_ai.workflows.errors import WorkflowConflictError, WorkflowInvalidStateError
from nexus_ai.workflows.state_machine import WorkflowRunState, WorkflowStepState

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _noop(key: str, *dependencies: str) -> WorkflowStepSpec:
    return WorkflowStepSpec(
        key=key,
        step_type=WorkflowStepType.NOOP,
        depends_on=dependencies,
        config=NoopStepConfig(output={"step": key}),
    )


async def _published(stack: Any, organization_id: Any, steps: tuple[WorkflowStepSpec, ...]) -> Any:
    definition = await stack.service.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"workflow.{str(organization_id)[:8]}",
            name="Durable workflow",
            steps=steps,
        ),
    )
    return definition, await stack.service.publish(organization_id, definition.id)


async def test_immutable_version_and_durable_noop_execution(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    definition, version = await _published(
        workflow_stack, organization.id, (_noop("first"), _noop("second", "first"))
    )
    updated = await workflow_stack.service.update_definition(
        organization.id,
        definition.id,
        UpdateWorkflowRequest(
            expected_revision=definition.revision,
            steps=(_noop("replacement"),),
        ),
    )
    assert updated.revision == 2
    assert [
        step.key
        for step in (await workflow_stack.service.get_version(organization.id, version.id)).steps
    ] == [
        "first",
        "second",
    ]

    run = await workflow_stack.service.start_run(
        organization.id,
        StartWorkflowRunRequest(
            workflow_version_id=version.id,
            input={"order": "123"},
            idempotency_key="workflow-run-123",
        ),
    )
    assert run.state is WorkflowRunState.RUNNING
    run = await workflow_stack.service.execute_next(principal, run.id)
    assert run is not None and run.state is WorkflowRunState.RUNNING
    run = await workflow_stack.service.execute_next(principal, run.id)
    assert run is not None and run.state is WorkflowRunState.COMPLETED
    assert await workflow_stack.service.execute_next(principal, run.id) is None
    steps = await workflow_stack.service.list_steps(organization.id, run.id)
    assert [step.state for step in steps] == [
        WorkflowStepState.COMPLETED,
        WorkflowStepState.COMPLETED,
    ]
    assert len(await workflow_stack.service.transitions(organization.id, run.id, limit=100)) >= 6


async def test_start_idempotency_and_payload_conflict(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, version = await _published(workflow_stack, organization.id, (_noop("only"),))
    request = StartWorkflowRunRequest(
        workflow_version_id=version.id, input={"same": True}, idempotency_key="same-logical-run"
    )
    first = await workflow_stack.service.start_run(organization.id, request)
    second = await workflow_stack.service.start_run(organization.id, request)
    assert first.id == second.id
    with pytest.raises(WorkflowConflictError):
        await workflow_stack.service.start_run(
            organization.id,
            StartWorkflowRunRequest(
                workflow_version_id=version.id,
                input={"same": False},
                idempotency_key="same-logical-run",
            ),
        )


async def test_condition_skips_unselected_branch(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    condition = WorkflowStepSpec(
        key="choose",
        step_type=WorkflowStepType.CONDITION,
        config=ConditionStepConfig(
            path="input.priority",
            operator=ConditionOperator.EQUALS,
            value="high",
            then_steps=("urgent",),
            else_steps=("normal",),
        ),
    )
    _, version = await _published(
        workflow_stack,
        organization.id,
        (condition, _noop("urgent", "choose"), _noop("normal", "choose")),
    )
    run = await workflow_stack.service.start_run(
        organization.id,
        StartWorkflowRunRequest(workflow_version_id=version.id, input={"priority": "high"}),
    )
    await workflow_stack.service.execute_next(principal, run.id)
    steps = {
        item.step_key: item
        for item in await workflow_stack.service.list_steps(organization.id, run.id)
    }
    assert steps["urgent"].state is WorkflowStepState.READY
    assert steps["normal"].state is WorkflowStepState.SKIPPED


async def test_pause_resume_cancel_and_terminal_absorption(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, version = await _published(workflow_stack, organization.id, (_noop("only"),))
    run = await workflow_stack.service.start_run(
        organization.id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )
    paused = await workflow_stack.service.pause_run(organization.id, run.id)
    assert paused.state is WorkflowRunState.PAUSED
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    resumed = await workflow_stack.service.resume_run(organization.id, run.id)
    assert resumed.state is WorkflowRunState.RUNNING
    cancelled = await workflow_stack.service.cancel_run(organization.id, run.id)
    assert cancelled.state is WorkflowRunState.CANCELLED
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    with pytest.raises(WorkflowInvalidStateError):
        await workflow_stack.service.resume_run(organization.id, run.id)


async def test_all_workflow_tables_are_rls_forced(workflow_stack: Any) -> None:
    async with workflow_stack.database.transaction() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname LIKE 'workflow_%' AND relkind='r' ORDER BY relname"
                )
            )
        ).all()
    assert len(rows) == 6
    assert all(row[1] and row[2] for row in rows)


async def test_tool_step_uses_real_p08_and_one_semantic_dispatch(
    workflow_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
) -> None:
    from tests.integration.test_agent_service import _register_tool

    organization = await make_organization()
    principal = await make_tool_principal(organization)
    await _register_tool(workflow_stack.agent_stack, organization.id, mock_http_server)
    hits = {"count": 0}

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, str]]:
        hits["count"] += 1
        return 200, {"id": "contact-1"}

    mock_http_server.set_handler(handler)
    step = WorkflowStepSpec(
        key="fetch",
        step_type=WorkflowStepType.TOOL,
        config=ToolStepConfig(tool_key="crm.get", arguments={"path_params": {"id": "contact-1"}}),
    )
    _, version = await _published(workflow_stack, organization.id, (step,))
    run = await workflow_stack.service.start_run(
        organization.id,
        StartWorkflowRunRequest(workflow_version_id=version.id),
    )
    completed = await workflow_stack.service.execute_next(principal, run.id)
    assert completed is not None and completed.state is WorkflowRunState.COMPLETED
    assert hits["count"] == 1
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        count = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
    assert count == 1


async def test_agent_step_uses_real_p13_without_provider_bypass(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    from nexus_ai.agents.models.fake import FakeModelTurn
    from tests.integration.test_agent_service import _provision

    organization = await make_organization()
    principal = await make_tool_principal(organization)
    agent = await _provision(workflow_stack.agent_stack, organization.id)
    workflow_stack.agent_stack.script.append(FakeModelTurn(content="governed answer"))
    step = WorkflowStepSpec(
        key="reason",
        step_type=WorkflowStepType.AGENT,
        config=AgentStepConfig(agent_id=agent.id, prompt="Decide safely"),
    )
    _, version = await _published(workflow_stack, organization.id, (step,))
    run = await workflow_stack.service.start_run(
        organization.id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )
    completed = await workflow_stack.service.execute_next(principal, run.id)
    assert completed is not None and completed.state is WorkflowRunState.COMPLETED
    steps = await workflow_stack.service.list_steps(organization.id, run.id)
    assert steps[0].output == {"content": "governed answer", "finish_reason": "STOP"}
    assert steps[0].external_reference is not None


async def test_cross_tenant_definition_version_and_run_are_invisible(
    workflow_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.workflows.errors import (
        WorkflowNotFoundError,
        WorkflowRunNotFoundError,
        WorkflowVersionNotFoundError,
    )

    tenant_a = await make_organization()
    tenant_b = await make_organization()
    definition, version = await _published(workflow_stack, tenant_a.id, (_noop("only"),))
    run = await workflow_stack.service.start_run(
        tenant_a.id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )
    with pytest.raises(WorkflowNotFoundError):
        await workflow_stack.service.get_definition(tenant_b.id, definition.id)
    with pytest.raises(WorkflowVersionNotFoundError):
        await workflow_stack.service.get_version(tenant_b.id, version.id)
    with pytest.raises(WorkflowRunNotFoundError):
        await workflow_stack.service.get_run(tenant_b.id, run.id)


async def test_retry_is_persisted_bounded_and_exhaustion_fails_run(
    workflow_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.workflows.repository import WorkflowRepository
    from nexus_ai.workflows.entities import RetryPolicy

    organization = await make_organization()
    step = WorkflowStepSpec(
        key="retry",
        step_type=WorkflowStepType.NOOP,
        config=NoopStepConfig(output={}),
        retry=RetryPolicy(max_attempts=2, retryable_codes=("NXS_TEST_RETRYABLE",)),
    )
    _, version = await _published(workflow_stack, organization.id, (step,))
    run = await workflow_stack.service.start_run(
        organization.id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )
    first = await workflow_stack.service.claim_next(organization.id, run.id)
    assert first is not None
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        retried = await WorkflowRepository(tenant).fail_step(
            first, error_code="NXS_TEST_RETRYABLE", retryable=True
        )
    assert retried.state is WorkflowRunState.RUNNING
    second = await workflow_stack.service.claim_next(organization.id, run.id)
    assert second is not None and second.step.attempt_count == 2
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        failed = await WorkflowRepository(tenant).fail_step(
            second, error_code="NXS_TEST_RETRYABLE", retryable=True
        )
    assert failed.state is WorkflowRunState.FAILED
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None


async def test_active_step_may_finish_while_paused_but_successor_waits_for_resume(
    workflow_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.workflows.repository import WorkflowRepository

    organization = await make_organization()
    _, version = await _published(
        workflow_stack, organization.id, (_noop("active"), _noop("later", "active"))
    )
    run = await workflow_stack.service.start_run(
        organization.id, StartWorkflowRunRequest(workflow_version_id=version.id)
    )
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None
    await workflow_stack.service.pause_run(organization.id, run.id)
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        paused = await WorkflowRepository(tenant).finish_step(claim, output={"done": True})
    assert paused.state is WorkflowRunState.PAUSED
    steps = await workflow_stack.service.list_steps(organization.id, run.id)
    assert [step.state for step in steps] == [
        WorkflowStepState.COMPLETED,
        WorkflowStepState.PENDING,
    ]
    resumed = await workflow_stack.service.resume_run(organization.id, run.id)
    assert resumed.state is WorkflowRunState.RUNNING
    steps = await workflow_stack.service.list_steps(organization.id, run.id)
    assert steps[1].state is WorkflowStepState.READY
