"""Strict P04 Campaign event payloads."""

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class CampaignEventPayload(EventPayload):
    campaign_id: UUID
    state: str
    reason_code: str | None = None
    count: int | None = None


class CampaignRecipientEventPayload(CampaignEventPayload):
    campaign_run_id: UUID
    recipient_id: UUID


def _event_model(name: str, base: type[EventPayload]) -> type[EventPayload]:
    return type(
        name.title().replace(".", "") + "V1",
        (base,),
        {
            "EVENT_TYPE": name,
            "VERSION": 1,
            "__annotations__": {"EVENT_TYPE": ClassVar[str], "VERSION": ClassVar[int]},
            "__module__": __name__,
        },
    )


for _event_type in (
    "campaign.created",
    "campaign.prepared",
    "campaign.scheduled",
    "campaign.started",
    "campaign.paused",
    "campaign.resumed",
    "campaign.cancelled",
    "campaign.completed",
    "campaign.failed",
):
    EVENT_REGISTRY.register(_event_type, 1, _event_model(_event_type, CampaignEventPayload))

for _event_type in (
    "campaign.recipient.eligible",
    "campaign.recipient.suppressed",
    "campaign.recipient.claimed",
    "campaign.recipient.workflow_started",
    "campaign.recipient.dispatched",
    "campaign.recipient.failed",
):
    EVENT_REGISTRY.register(
        _event_type, 1, _event_model(_event_type, CampaignRecipientEventPayload)
    )
