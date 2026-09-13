"""Bounded, side-effect-free workflow condition evaluation."""

from typing import Any

from nexus_ai.workflows.entities import ConditionOperator, ConditionStepConfig
from nexus_ai.workflows.errors import WorkflowInvalidDefinitionError

_MISSING = object()


def evaluate_condition(
    condition: ConditionStepConfig, *, workflow_input: dict[str, Any], step_outputs: dict[str, Any]
) -> bool:
    root: Any = {"input": workflow_input, "steps": step_outputs}
    value = _resolve(root, condition.path)
    operator = condition.operator
    if operator is ConditionOperator.EXISTS:
        return value is not _MISSING
    if operator is ConditionOperator.NOT_EXISTS:
        return value is _MISSING
    if value is _MISSING:
        return False
    expected = condition.value
    if operator is ConditionOperator.EQUALS:
        return bool(value == expected)
    if operator is ConditionOperator.NOT_EQUALS:
        return bool(value != expected)
    if operator is ConditionOperator.IN:
        return isinstance(expected, (list, tuple)) and value in expected
    if operator is ConditionOperator.NOT_IN:
        return isinstance(expected, (list, tuple)) and value not in expected
    if operator is ConditionOperator.GREATER_THAN:
        return _ordered(value, expected, greater=True)
    if operator is ConditionOperator.LESS_THAN:
        return _ordered(value, expected, greater=False)
    raise WorkflowInvalidDefinitionError("unsupported condition operator")


def _resolve(root: Any, path: str) -> Any:
    current = root
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _ordered(left: Any, right: Any, *, greater: bool) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float, str)) or not isinstance(right, type(left)):
        return False
    return bool(left > right if greater else left < right)
