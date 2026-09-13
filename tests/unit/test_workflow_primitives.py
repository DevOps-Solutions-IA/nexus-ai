"""Deterministic P14 graph, condition and state-machine contracts."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.workflows.conditions import evaluate_condition
from nexus_ai.workflows.entities import (
    AgentStepConfig,
    ConditionOperator,
    ConditionStepConfig,
    NoopStepConfig,
    ToolStepConfig,
    WorkflowStepSpec,
    WorkflowStepType,
    validate_workflow_graph,
)
from nexus_ai.workflows.errors import WorkflowInvalidDefinitionError, WorkflowInvalidStateError
from nexus_ai.workflows.state_machine import (
    WorkflowRunState,
    WorkflowStepState,
    require_step_transition,
    require_workflow_transition,
)


def _noop(key: str, *dependencies: str) -> WorkflowStepSpec:
    return WorkflowStepSpec(
        key=key,
        step_type=WorkflowStepType.NOOP,
        depends_on=dependencies,
        config=NoopStepConfig(output={"key": key}),
    )


def test_graph_is_deterministically_topologically_sorted() -> None:
    assert validate_workflow_graph((_noop("last", "a", "b"), _noop("b"), _noop("a"))) == (
        "a",
        "b",
        "last",
    )


@pytest.mark.parametrize(
    "steps",
    [
        (_noop("a", "missing"),),
        (_noop("a", "b"), _noop("b", "a")),
        (_noop("same"), _noop("same")),
    ],
)
def test_invalid_graphs_fail_closed(steps: tuple[WorkflowStepSpec, ...]) -> None:
    with pytest.raises(WorkflowInvalidDefinitionError):
        validate_workflow_graph(steps)


def test_condition_requires_declared_dependent_branches() -> None:
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
    with pytest.raises(WorkflowInvalidDefinitionError):
        validate_workflow_graph((condition, _noop("urgent"), _noop("normal", "choose")))


@pytest.mark.parametrize(
    ("operator", "expected", "result"),
    [
        (ConditionOperator.EQUALS, 3, True),
        (ConditionOperator.NOT_EQUALS, 4, True),
        (ConditionOperator.GREATER_THAN, 2, True),
        (ConditionOperator.LESS_THAN, 4, True),
        (ConditionOperator.IN, [1, 3], True),
    ],
)
def test_condition_evaluation_is_bounded_and_deterministic(
    operator: ConditionOperator, expected: object, result: bool
) -> None:
    config = ConditionStepConfig(
        path="input.count",
        operator=operator,
        value=expected,
        then_steps=(),
        else_steps=(),
    )
    assert evaluate_condition(config, workflow_input={"count": 3}, step_outputs={}) is result
    assert evaluate_condition(config, workflow_input={"count": 3}, step_outputs={}) is result


def test_only_governed_step_types_are_accepted() -> None:
    with pytest.raises(ValidationError):
        WorkflowStepSpec.model_validate(
            {"key": "bad", "step_type": "SHELL", "config": {"kind": "SHELL"}}
        )
    with pytest.raises(ValidationError):
        ToolStepConfig.model_validate({"tool_key": "safe.tool", "url": "https://attacker.test"})
    with pytest.raises(ValidationError):
        AgentStepConfig.model_validate(
            {"agent_id": str(uuid4()), "prompt": "hi", "provider": "openai"}
        )


@pytest.mark.parametrize(
    "terminal",
    [WorkflowRunState.COMPLETED, WorkflowRunState.FAILED, WorkflowRunState.CANCELLED],
)
def test_workflow_terminal_states_are_absorbing(terminal: WorkflowRunState) -> None:
    with pytest.raises(WorkflowInvalidStateError):
        require_workflow_transition(terminal, WorkflowRunState.RUNNING)


@pytest.mark.parametrize(
    "terminal",
    [
        WorkflowStepState.COMPLETED,
        WorkflowStepState.FAILED,
        WorkflowStepState.SKIPPED,
        WorkflowStepState.CANCELLED,
    ],
)
def test_step_terminal_states_are_absorbing(terminal: WorkflowStepState) -> None:
    with pytest.raises(WorkflowInvalidStateError):
        require_step_transition(terminal, WorkflowStepState.READY)
