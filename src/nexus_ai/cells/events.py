"""P04 version-one tenant placement events."""

from typing import ClassVar
from uuid import UUID

from nexus_ai.cells.contracts import Generation, PlacementState, ReasonCode
from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class AssignmentCreated(EventPayload):
    EVENT_TYPE: ClassVar[str] = "cell.assignment.created"
    VERSION: ClassVar[int] = 1
    placement_id: UUID
    cell_id: UUID
    assignment_generation: Generation
    state: PlacementState
    reason_code: ReasonCode


class AssignmentSuspended(AssignmentCreated):
    EVENT_TYPE: ClassVar[str] = "cell.assignment.suspended"


class AssignmentResumed(AssignmentCreated):
    EVENT_TYPE: ClassVar[str] = "cell.assignment.resumed"


for event_model in (AssignmentCreated, AssignmentSuspended, AssignmentResumed):
    EVENT_REGISTRY.register(event_model.EVENT_TYPE, event_model.VERSION, event_model)
