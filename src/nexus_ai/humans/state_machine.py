"""Closed, absorbing P17 state machines."""

from nexus_ai.humans.entities import AssignmentState, WorkItemState
from nexus_ai.humans.errors import HumanInvalidStateError

_WORK_TRANSITIONS = {
    WorkItemState.QUEUED: {WorkItemState.CLAIMED, WorkItemState.CANCELLED},
    WorkItemState.CLAIMED: {
        WorkItemState.ACCEPTED,
        WorkItemState.QUEUED,
        WorkItemState.CANCELLED,
    },
    WorkItemState.ACCEPTED: {
        WorkItemState.ACTIVE,
        WorkItemState.QUEUED,
        WorkItemState.CANCELLED,
    },
    WorkItemState.ACTIVE: {
        WorkItemState.AI_RETURN_PENDING,
        WorkItemState.WRAP_UP,
        WorkItemState.QUEUED,
        WorkItemState.CANCELLED,
    },
    WorkItemState.AI_RETURN_PENDING: {WorkItemState.QUEUED, WorkItemState.COMPLETED},
    WorkItemState.WRAP_UP: {WorkItemState.COMPLETED, WorkItemState.QUEUED},
    WorkItemState.COMPLETED: set(),
    WorkItemState.CANCELLED: set(),
}

_ASSIGNMENT_TRANSITIONS = {
    AssignmentState.CLAIMED: {
        AssignmentState.ACCEPTED,
        AssignmentState.RELEASED,
        AssignmentState.TRANSFERRED,
    },
    AssignmentState.ACCEPTED: {
        AssignmentState.ACTIVE,
        AssignmentState.RELEASED,
        AssignmentState.TRANSFERRED,
    },
    AssignmentState.ACTIVE: {
        AssignmentState.RELEASED,
        AssignmentState.TRANSFERRED,
        AssignmentState.COMPLETED,
    },
    AssignmentState.RELEASED: set(),
    AssignmentState.TRANSFERRED: set(),
    AssignmentState.COMPLETED: set(),
}


def require_work_transition(current: WorkItemState, target: WorkItemState) -> None:
    if target not in _WORK_TRANSITIONS[current]:
        raise HumanInvalidStateError(f"work item transition {current} -> {target} is forbidden")


def require_assignment_transition(current: AssignmentState, target: AssignmentState) -> None:
    if target not in _ASSIGNMENT_TRANSITIONS[current]:
        raise HumanInvalidStateError(f"assignment transition {current} -> {target} is forbidden")
