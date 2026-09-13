"""Strict P04 Scheduler event payloads."""

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _ScheduleEvent(EventPayload):
    schedule_id: UUID
    state: str
    reason_code: str | None = None


class _OccurrenceEvent(_ScheduleEvent):
    occurrence_id: UUID
    scheduled_for: str


def _register(name: str, base: type[EventPayload]) -> type[EventPayload]:
    return type(
        name.title().replace(".", "").replace("_", "") + "V1",
        (base,),
        {
            "EVENT_TYPE": name,
            "VERSION": 1,
            "__annotations__": {"EVENT_TYPE": ClassVar[str], "VERSION": ClassVar[int]},
            "__module__": __name__,
        },
    )


for _event in (
    "scheduler.schedule.created",
    "scheduler.schedule.activated",
    "scheduler.schedule.paused",
    "scheduler.schedule.resumed",
    "scheduler.schedule.cancelled",
):
    EVENT_REGISTRY.register(_event, 1, _register(_event, _ScheduleEvent))

for _event in (
    "scheduler.occurrence.created",
    "scheduler.occurrence.claimed",
    "scheduler.occurrence.dispatched",
    "scheduler.occurrence.failed",
    "scheduler.occurrence.skipped",
    "scheduler.occurrence.cancelled",
):
    EVENT_REGISTRY.register(_event, 1, _register(_event, _OccurrenceEvent))
