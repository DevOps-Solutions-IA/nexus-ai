"""Tool Engine event payloads (NXS-TOOL-001 with NXS-EVENT-009).

Registered in the canonical P04 payload registry. Payloads carry IDs and safe lifecycle
facts ONLY — never tool arguments, a downstream response body, a secret, a URL or a
provider detail. The trusted ``organization_id`` comes from the envelope.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


@EVENT_REGISTRY.payload_model("tools.registered")
class ToolRegisteredV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "tools.registered"
    VERSION: ClassVar[int] = 1

    tool_id: UUID
    tool_key: str
    risk_class: str
    side_effect_class: str


@EVENT_REGISTRY.payload_model("tools.updated")
class ToolUpdatedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "tools.updated"
    VERSION: ClassVar[int] = 1

    tool_id: UUID
    tool_key: str
    version: int


@EVENT_REGISTRY.payload_model("tools.disabled")
class ToolDisabledV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "tools.disabled"
    VERSION: ClassVar[int] = 1

    tool_id: UUID
    tool_key: str
    status: str


@EVENT_REGISTRY.payload_model("tools.invocation.completed")
class ToolInvocationCompletedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "tools.invocation.completed"
    VERSION: ClassVar[int] = 1

    tool_id: UUID
    tool_key: str
    tool_version: int
    duration_ms: int
    replayed: bool


@EVENT_REGISTRY.payload_model("tools.invocation.failed")
class ToolInvocationFailedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "tools.invocation.failed"
    VERSION: ClassVar[int] = 1

    tool_id: UUID
    tool_key: str
    tool_version: int
    result_class: str
    error_code: str
    downstream_code: str | None = None
