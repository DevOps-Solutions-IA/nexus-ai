"""Strict P04 event contracts for P17."""

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class HumanEventPayload(EventPayload):
    entity_id: UUID
    state: str
    reason_code: str | None = None
    actor_user_id: UUID | None = None


def _event_model(name: str) -> type[EventPayload]:
    return type(
        name.title().replace(".", "") + "V1",
        (HumanEventPayload,),
        {
            "EVENT_TYPE": name,
            "VERSION": 1,
            "__annotations__": {"EVENT_TYPE": ClassVar[str], "VERSION": ClassVar[int]},
            "__module__": __name__,
        },
    )


for _event_type in (
    "human.work.queued",
    "human.work.claimed",
    "human.work.accepted",
    "human.work.transferred",
    "human.work.requeued",
    "human.work.completed",
    "human.work.cancelled",
    "human.handoff.ai_to_human",
    "human.handoff.human_to_ai",
    "human.presence.changed",
    "human.supervisor.released",
):
    EVENT_REGISTRY.register(_event_type, 1, _event_model(_event_type))
