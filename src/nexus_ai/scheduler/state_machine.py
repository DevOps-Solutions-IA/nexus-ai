"""Closed, absorbing Scheduler lifecycle state machines."""

from enum import StrEnum

from nexus_ai.scheduler.errors import ScheduleInvalidStateError


class ScheduleState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OccurrenceState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DISPATCHED = "DISPATCHED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


SCHEDULE_TERMINAL = frozenset({ScheduleState.COMPLETED, ScheduleState.CANCELLED})
OCCURRENCE_TERMINAL = frozenset(
    {
        OccurrenceState.DISPATCHED,
        OccurrenceState.FAILED,
        OccurrenceState.SKIPPED,
        OccurrenceState.CANCELLED,
    }
)

_SCHEDULE_TRANSITIONS = {
    ScheduleState.DRAFT: {ScheduleState.ACTIVE, ScheduleState.CANCELLED},
    ScheduleState.ACTIVE: {
        ScheduleState.PAUSED,
        ScheduleState.COMPLETED,
        ScheduleState.CANCELLED,
    },
    ScheduleState.PAUSED: {ScheduleState.ACTIVE, ScheduleState.CANCELLED},
    ScheduleState.COMPLETED: set(),
    ScheduleState.CANCELLED: set(),
}
_OCCURRENCE_TRANSITIONS = {
    OccurrenceState.PENDING: {
        OccurrenceState.CLAIMED,
        OccurrenceState.SKIPPED,
        OccurrenceState.CANCELLED,
    },
    OccurrenceState.CLAIMED: {OccurrenceState.DISPATCHED, OccurrenceState.FAILED},
    OccurrenceState.DISPATCHED: set(),
    OccurrenceState.FAILED: set(),
    OccurrenceState.SKIPPED: set(),
    OccurrenceState.CANCELLED: set(),
}


def require_schedule_transition(current: ScheduleState, target: ScheduleState) -> None:
    if target not in _SCHEDULE_TRANSITIONS[current]:
        raise ScheduleInvalidStateError(f"schedule cannot transition from {current} to {target}")


def require_occurrence_transition(current: OccurrenceState, target: OccurrenceState) -> None:
    if target not in _OCCURRENCE_TRANSITIONS[current]:
        raise ScheduleInvalidStateError(f"occurrence cannot transition from {current} to {target}")
