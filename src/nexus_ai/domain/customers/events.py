"""Customer and conversation event payloads (NXS-CUSTOMER-001 with NXS-EVENT-009).

Registered in the canonical P04 payload registry. Payloads minimize PII aggressively:
customer/identity/conversation IDs and lifecycle facts only — never raw emails,
phones or message bodies.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


@EVENT_REGISTRY.payload_model("customers.created")
class CustomerCreatedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "customers.created"
    VERSION: ClassVar[int] = 1

    customer_id: UUID
    initial_identity_id: UUID
    identity_type: str


@EVENT_REGISTRY.payload_model("customers.identity.linked")
class CustomerIdentityLinkedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "customers.identity.linked"
    VERSION: ClassVar[int] = 1

    customer_id: UUID
    identity_id: UUID
    identity_type: str


@EVENT_REGISTRY.payload_model("customers.identity.status_changed")
class CustomerIdentityStatusChangedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "customers.identity.status_changed"
    VERSION: ClassVar[int] = 1

    customer_id: UUID
    identity_id: UUID
    verification_state: str


@EVENT_REGISTRY.payload_model("conversations.opened")
class ConversationOpenedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "conversations.opened"
    VERSION: ClassVar[int] = 1

    conversation_id: UUID
    customer_id: UUID | None
    channel: str


@EVENT_REGISTRY.payload_model("conversations.closed")
class ConversationClosedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "conversations.closed"
    VERSION: ClassVar[int] = 1

    conversation_id: UUID
    customer_id: UUID | None
    channel: str
