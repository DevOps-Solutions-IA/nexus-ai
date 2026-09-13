"""Conditional branch propagation and convergence against real PostgreSQL."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.domain.workflows.repository import WorkflowRepository
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
from nexus_ai.workflows.errors import WorkflowExecutionFencedError
from nexus_ai.workflows.state_machine import WorkflowRunState, WorkflowStepState

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _noop(key: str, *dependencies: str) -> WorkflowStepSpec:
    return WorkflowStepSpec(
        key=key,
        step_type=WorkflowStepType.NOOP,
        depends_on=dependencies,
        config=NoopStepConfig(output={"step": key}),
    )


def _condition(
    key: str,
    *,
    dependencies: tuple[str, ...] = (),
    then_steps: tuple[str, ...],
    else_steps: tuple[str, ...],
    path: str = "input.selected",
    value: object = "yes",
) -> WorkflowStepSpec:
    return WorkflowStepSpec(
        key=key,
        step_type=WorkflowStepType.CONDITION,
        depends_on=dependencies,
        config=ConditionStepConfig(
            path=path,
            operator=ConditionOperator.EQUALS,
            value=value,
            then_steps=then_steps,
            else_steps=else_steps,
        ),
    )


async def _start(
    stack: Any,
    organization_id: uuid.UUID,
    steps: tuple[WorkflowStepSpec, ...],
    *,
    workflow_input: dict[str, object] | None = None,
) -> Any:
    definition = await stack.service.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"branch.{uuid.uuid4().hex[:12]}",
            name="Conditional branch",
            steps=steps,
        ),
    )
    version = await stack.service.publish(organization_id, definition.id)
    return await stack.service.start_run(
        organization_id,
        StartWorkflowRunRequest(
            workflow_version_id=version.id,
            input={"selected": "yes"} if workflow_input is None else workflow_input,
        ),
    )


async def _states(stack: Any, organization_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    return {step.step_key: step for step in await stack.service.list_steps(organization_id, run_id)}


async def test_multilevel_exclusive_branch_propagates_skip(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("a1",), else_steps=("b1",)),
            _noop("a1", "choose"),
            _noop("a2", "a1"),
            _noop("b1", "choose"),
            _noop("b2", "b1"),
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["choose"].output == {
        "selected": True,
        "selected_steps": ["a1"],
        "skipped_steps": ["b1"],
    }
    assert steps["a1"].state is WorkflowStepState.READY
    assert steps["a2"].state is WorkflowStepState.PENDING
    assert steps["b1"].state is WorkflowStepState.SKIPPED
    assert steps["b2"].state is WorkflowStepState.SKIPPED


async def test_deep_exclusive_branch_propagates_skip_to_fixed_point(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("a",), else_steps=("b",)),
            _noop("a", "choose"),
            _noop("a2", "a"),
            _noop("a3", "a2"),
            _noop("b", "choose"),
            _noop("b2", "b"),
            _noop("b3", "b2"),
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert [steps[key].state for key in ("b", "b2", "b3")] == [
        WorkflowStepState.SKIPPED,
        WorkflowStepState.SKIPPED,
        WorkflowStepState.SKIPPED,
    ]
    assert steps["a"].state is WorkflowStepState.READY


async def test_simple_join_waits_for_selected_branch_then_becomes_ready(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("a",), else_steps=("b",)),
            _noop("a", "choose"),
            _noop("b", "choose"),
            _noop("join", "a", "b"),
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["join"].state is WorkflowStepState.PENDING
    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["join"].state is WorkflowStepState.READY


async def test_multilevel_join_and_successor_follow_active_branch(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("a1",), else_steps=("b1",)),
            _noop("a1", "choose"),
            _noop("a2", "a1"),
            _noop("b1", "choose"),
            _noop("b2", "b1"),
            _noop("join", "a2", "b2"),
            _noop("after_join", "join"),
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    await workflow_stack.service.execute_next(principal, run.id)
    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["b1"].state is WorkflowStepState.SKIPPED
    assert steps["b2"].state is WorkflowStepState.SKIPPED
    assert steps["join"].state is WorkflowStepState.READY
    assert steps["after_join"].state is WorkflowStepState.PENDING
    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["after_join"].state is WorkflowStepState.READY


async def test_nested_condition_in_active_branch_selects_once(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("outer", then_steps=("inner",), else_steps=("outer_no",)),
            _condition(
                "inner",
                dependencies=("outer",),
                then_steps=("c",),
                else_steps=("d",),
            ),
            _noop("outer_no", "outer"),
            _noop("c", "inner"),
            _noop("d", "inner"),
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["outer_no"].state is WorkflowStepState.SKIPPED
    assert steps["inner"].state is WorkflowStepState.COMPLETED
    assert steps["c"].state is WorkflowStepState.READY
    assert steps["d"].state is WorkflowStepState.SKIPPED


async def test_condition_inside_skipped_branch_never_evaluates_or_activates_children(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("outer", then_steps=("active",), else_steps=("inner",)),
            _noop("active", "outer"),
            _condition(
                "inner",
                dependencies=("outer",),
                then_steps=("c",),
                else_steps=("d",),
            ),
            _noop("c", "inner"),
            _noop("d", "inner"),
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["inner"].state is WorkflowStepState.SKIPPED
    assert steps["inner"].output is None
    assert steps["c"].state is WorkflowStepState.SKIPPED
    assert steps["d"].state is WorkflowStepState.SKIPPED


async def test_skipped_tool_descendant_never_reaches_p08(
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
        return 200, {"id": "unexpected"}

    mock_http_server.set_handler(handler)
    tool = WorkflowStepSpec(
        key="tool",
        step_type=WorkflowStepType.TOOL,
        depends_on=("inactive",),
        config=ToolStepConfig(tool_key="crm.get", arguments={"path_params": {"id": "one"}}),
    )
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("active",), else_steps=("inactive",)),
            _noop("active", "choose"),
            _noop("inactive", "choose"),
            tool,
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    await workflow_stack.service.execute_next(principal, run.id)
    assert await workflow_stack.service.execute_next(principal, run.id) is None
    assert hits == 0
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["tool"].state is WorkflowStepState.SKIPPED
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        count = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
    assert count == 0


async def test_skipped_agent_descendant_never_reaches_p13(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    from tests.integration.test_agent_service import _provision

    organization = await make_organization()
    principal = await make_tool_principal(organization)
    agent = await _provision(workflow_stack.agent_stack, organization.id)
    agent_step = WorkflowStepSpec(
        key="agent",
        step_type=WorkflowStepType.AGENT,
        depends_on=("inactive",),
        config=AgentStepConfig(agent_id=agent.id, prompt="Must not execute"),
    )
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("active",), else_steps=("inactive",)),
            _noop("active", "choose"),
            _noop("inactive", "choose"),
            agent_step,
        ),
    )

    await workflow_stack.service.execute_next(principal, run.id)
    await workflow_stack.service.execute_next(principal, run.id)
    assert await workflow_stack.service.execute_next(principal, run.id) is None
    assert workflow_stack.agent_stack.provider.calls == []
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["agent"].state is WorkflowStepState.SKIPPED


async def test_duplicate_condition_replay_cannot_flip_durable_outcome(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("yes",), else_steps=("no",)),
            _noop("yes", "choose"),
            _noop("no", "choose"),
            _noop("no_child", "no"),
        ),
    )
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        await WorkflowRepository(tenant).finish_condition(
            claim,
            output={
                "selected": True,
                "selected_steps": ["yes"],
                "skipped_steps": ["no"],
            },
            skipped_step_keys=("no",),
        )
    async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(WorkflowExecutionFencedError):
            await WorkflowRepository(tenant).finish_condition(
                claim,
                output={
                    "selected": False,
                    "selected_steps": ["no"],
                    "skipped_steps": ["yes"],
                },
                skipped_step_keys=("yes",),
            )
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["choose"].output == {
        "selected": True,
        "selected_steps": ["yes"],
        "skipped_steps": ["no"],
    }
    assert steps["yes"].state is WorkflowStepState.READY
    assert steps["no"].state is WorkflowStepState.SKIPPED
    assert steps["no_child"].state is WorkflowStepState.SKIPPED


async def test_two_worker_condition_race_has_one_branch_outcome(
    workflow_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("yes",), else_steps=("no",)),
            _noop("yes", "choose"),
            _noop("no", "choose"),
            _noop("no_child", "no"),
        ),
    )
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None
    ready = asyncio.Event()
    release = asyncio.Event()
    arrivals = 0
    arrivals_lock = asyncio.Lock()

    async def finish(selected: bool) -> Any:
        nonlocal arrivals
        async with arrivals_lock:
            arrivals += 1
            if arrivals == 2:
                ready.set()
        await release.wait()
        skipped = ("no",) if selected else ("yes",)
        async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
            return await WorkflowRepository(tenant).finish_condition(
                claim,
                output={"selected": selected},
                skipped_step_keys=skipped,
            )

    first = asyncio.create_task(finish(True))
    second = asyncio.create_task(finish(False))
    await ready.wait()
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, WorkflowExecutionFencedError) for outcome in outcomes) == 1
    steps = await _states(workflow_stack, organization.id, run.id)
    branch_states = {steps["yes"].state, steps["no"].state}
    assert branch_states == {WorkflowStepState.READY, WorkflowStepState.SKIPPED}
    if steps["no"].state is WorkflowStepState.SKIPPED:
        assert steps["no_child"].state is WorkflowStepState.SKIPPED


async def test_cancel_after_branch_resolution_never_reactivates_excluded_steps(
    workflow_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    run = await _start(
        workflow_stack,
        organization.id,
        (
            _condition("choose", then_steps=("yes",), else_steps=("no",)),
            _noop("yes", "choose"),
            _noop("no", "choose"),
            _noop("no_child", "no"),
        ),
    )
    await workflow_stack.service.execute_next(principal, run.id)

    cancelled = await workflow_stack.service.cancel_run(organization.id, run.id)

    assert cancelled.state is WorkflowRunState.CANCELLED
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    steps = await _states(workflow_stack, organization.id, run.id)
    assert steps["yes"].state is WorkflowStepState.CANCELLED
    assert steps["no"].state is WorkflowStepState.SKIPPED
    assert steps["no_child"].state is WorkflowStepState.SKIPPED
    assert all(step.state is not WorkflowStepState.READY for step in steps.values())
