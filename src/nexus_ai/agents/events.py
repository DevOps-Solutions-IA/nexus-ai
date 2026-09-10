"""AI Agent Runtime audit event payloads (NXS-P13 with NXS-EVENT-013).

Registered in the canonical NXS-P04 payload registry. Payloads carry IDs and safe
lifecycle / bounded-metric metadata ONLY — never a model API key, a raw endpoint, a raw
provider payload, a raw prompt, tool arguments or any model reasoning. The trusted
``organization_id`` comes from the envelope. There is no chain-of-thought field.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _AgentSessionEventV1(EventPayload):
    session_id: UUID
    agent_id: UUID
    model_profile_id: UUID
    channel: str
    state: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("agent.session.created")
class AgentSessionCreatedV1(_AgentSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.session.created"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("agent.session.started")
class AgentSessionStartedV1(_AgentSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.session.started"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("agent.session.completed")
class AgentSessionCompletedV1(_AgentSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.session.completed"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("agent.session.failed")
class AgentSessionFailedV1(_AgentSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.session.failed"
    VERSION: ClassVar[int] = 1
    error_code: str | None = None


@EVENT_REGISTRY.payload_model("agent.session.cancelled")
class AgentSessionCancelledV1(_AgentSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.session.cancelled"
    VERSION: ClassVar[int] = 1


class _AgentTurnEventV1(EventPayload):
    session_id: UUID
    turn_id: UUID
    sequence: int
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("agent.turn.started")
class AgentTurnStartedV1(_AgentTurnEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.turn.started"
    VERSION: ClassVar[int] = 1
    input_char_count: int = 0


@EVENT_REGISTRY.payload_model("agent.turn.completed")
class AgentTurnCompletedV1(_AgentTurnEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.turn.completed"
    VERSION: ClassVar[int] = 1
    finish_reason: str | None = None
    tool_iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@EVENT_REGISTRY.payload_model("agent.turn.failed")
class AgentTurnFailedV1(_AgentTurnEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.turn.failed"
    VERSION: ClassVar[int] = 1
    error_code: str | None = None
    latency_ms: int = 0


class _AgentToolEventV1(EventPayload):
    session_id: UUID
    turn_id: UUID
    tool_call_id: UUID
    tool_key: str
    iteration: int
    arguments_hash: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("agent.tool.requested")
class AgentToolRequestedV1(_AgentToolEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.tool.requested"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("agent.tool.completed")
class AgentToolCompletedV1(_AgentToolEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.tool.completed"
    VERSION: ClassVar[int] = 1
    result_class: str | None = None
    status_code: int | None = None
    latency_ms: int = 0


@EVENT_REGISTRY.payload_model("agent.tool.failed")
class AgentToolFailedV1(_AgentToolEventV1):
    EVENT_TYPE: ClassVar[str] = "agent.tool.failed"
    VERSION: ClassVar[int] = 1
    #: DENIED (allow-list / RBAC / policy) or FAILED (execution) — a stable class only.
    outcome: str = "FAILED"
    error_code: str | None = None
    latency_ms: int = 0


@EVENT_REGISTRY.payload_model("agent.response.ready")
class AgentResponseReadyV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "agent.response.ready"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    turn_id: UUID
    channel: str
    #: a CHARACTER COUNT — never the response body
    response_char_count: int = 0
    finish_reason: str | None = None
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("agent.usage.recorded")
class AgentUsageRecordedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "agent.usage.recorded"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_call_count: int = 0
    turn_count: int = 0
    latency_ms_total: int = 0
    correlation_id: str | None = None


SESSION_STATE_EVENT_TYPE: dict[str, str] = {
    "PENDING": "agent.session.created",
    "ACTIVE": "agent.session.started",
    "COMPLETED": "agent.session.completed",
    "FAILED": "agent.session.failed",
    "CANCELLED": "agent.session.cancelled",
}
