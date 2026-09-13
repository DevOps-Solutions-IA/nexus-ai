"""Explicit absorbing workflow and step state machines."""

from enum import StrEnum

from nexus_ai.workflows.errors import WorkflowInvalidStateError


class WorkflowDefinitionStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class WorkflowRunState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkflowStepState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


WORKFLOW_TERMINAL = frozenset(
    {WorkflowRunState.COMPLETED, WorkflowRunState.FAILED, WorkflowRunState.CANCELLED}
)
STEP_TERMINAL = frozenset(
    {
        WorkflowStepState.COMPLETED,
        WorkflowStepState.FAILED,
        WorkflowStepState.SKIPPED,
        WorkflowStepState.CANCELLED,
    }
)

_WORKFLOW_TRANSITIONS = {
    WorkflowRunState.PENDING: {WorkflowRunState.RUNNING, WorkflowRunState.CANCELLED},
    WorkflowRunState.RUNNING: {
        WorkflowRunState.PAUSED,
        WorkflowRunState.COMPLETED,
        WorkflowRunState.FAILED,
        WorkflowRunState.CANCELLED,
    },
    WorkflowRunState.PAUSED: {
        WorkflowRunState.RUNNING,
        WorkflowRunState.FAILED,
        WorkflowRunState.CANCELLED,
    },
    WorkflowRunState.COMPLETED: set(),
    WorkflowRunState.FAILED: set(),
    WorkflowRunState.CANCELLED: set(),
}

_STEP_TRANSITIONS = {
    WorkflowStepState.PENDING: {
        WorkflowStepState.READY,
        WorkflowStepState.SKIPPED,
        WorkflowStepState.CANCELLED,
    },
    WorkflowStepState.READY: {
        WorkflowStepState.RUNNING,
        WorkflowStepState.SKIPPED,
        WorkflowStepState.CANCELLED,
    },
    WorkflowStepState.RUNNING: {
        WorkflowStepState.READY,
        WorkflowStepState.COMPLETED,
        WorkflowStepState.FAILED,
        WorkflowStepState.CANCELLED,
    },
    WorkflowStepState.COMPLETED: set(),
    WorkflowStepState.FAILED: set(),
    WorkflowStepState.SKIPPED: set(),
    WorkflowStepState.CANCELLED: set(),
}


def require_workflow_transition(current: WorkflowRunState, target: WorkflowRunState) -> None:
    if target not in _WORKFLOW_TRANSITIONS[current]:
        raise WorkflowInvalidStateError(f"workflow cannot transition from {current} to {target}")


def require_step_transition(current: WorkflowStepState, target: WorkflowStepState) -> None:
    if target not in _STEP_TRANSITIONS[current]:
        raise WorkflowInvalidStateError(
            f"workflow step cannot transition from {current} to {target}"
        )
