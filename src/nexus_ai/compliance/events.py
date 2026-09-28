"""Typed P04 announcements with IDs and bounded lifecycle facts, never subject exports."""

from typing import ClassVar
from uuid import UUID

from pydantic import Field

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


@EVENT_REGISTRY.payload_model("compliance.state.changed")
class ComplianceStateChangedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "compliance.state.changed"
    VERSION: ClassVar[int] = 1

    resource_id: UUID
    resource_type: str = Field(max_length=32)
    state: str = Field(max_length=32)
