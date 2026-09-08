"""Messaging event payloads (NXS-P09 with NXS-EVENT-009).

Registered in the canonical P04 payload registry. Payloads carry IDs and safe lifecycle
metadata ONLY — never a message body, an address, a subject, a provider token, an auth
header or a raw provider payload. The trusted ``organization_id`` comes from the
envelope. Privacy: a body is never on the bus; a recipient address is never on the bus.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _MessageEventV1(EventPayload):
    message_id: UUID
    conversation_id: UUID
    channel: str
    direction: str
    provider: str
    provider_message_id: str | None = None


@EVENT_REGISTRY.payload_model("messaging.message.received")
class MessageReceivedV1(_MessageEventV1):
    EVENT_TYPE: ClassVar[str] = "messaging.message.received"
    VERSION: ClassVar[int] = 1

    customer_id: UUID | None = None
    replayed: bool = False


@EVENT_REGISTRY.payload_model("messaging.message.queued")
class MessageQueuedV1(_MessageEventV1):
    EVENT_TYPE: ClassVar[str] = "messaging.message.queued"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("messaging.message.sent")
class MessageSentV1(_MessageEventV1):
    EVENT_TYPE: ClassVar[str] = "messaging.message.sent"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("messaging.message.delivered")
class MessageDeliveredV1(_MessageEventV1):
    EVENT_TYPE: ClassVar[str] = "messaging.message.delivered"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("messaging.message.read")
class MessageReadV1(_MessageEventV1):
    EVENT_TYPE: ClassVar[str] = "messaging.message.read"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("messaging.message.failed")
class MessageFailedV1(_MessageEventV1):
    EVENT_TYPE: ClassVar[str] = "messaging.message.failed"
    VERSION: ClassVar[int] = 1

    error_code: str
    provider_code: str | None = None


#: status -> event type, for the delivery-callback path.
STATUS_EVENT_TYPE: dict[str, str] = {
    "QUEUED": "messaging.message.queued",
    "SENT": "messaging.message.sent",
    "DELIVERED": "messaging.message.delivered",
    "READ": "messaging.message.read",
    "FAILED": "messaging.message.failed",
}
