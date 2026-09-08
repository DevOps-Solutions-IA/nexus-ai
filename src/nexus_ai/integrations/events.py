"""Integration Hub event payloads (NXS-INT-001 with NXS-EVENT-009).

Registered in the canonical P04 payload registry. Payloads carry IDs and safe lifecycle
facts ONLY — never a base URL with credentials, a secret, an upstream response body, a
header block or a webhook payload. The trusted ``organization_id`` comes from the
envelope (server-side context), never from a caller field.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


@EVENT_REGISTRY.payload_model("integrations.created")
class IntegrationCreatedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "integrations.created"
    VERSION: ClassVar[int] = 1

    integration_id: UUID
    slug: str
    integration_type: str


@EVENT_REGISTRY.payload_model("integrations.updated")
class IntegrationUpdatedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "integrations.updated"
    VERSION: ClassVar[int] = 1

    integration_id: UUID
    config_revision: int


@EVENT_REGISTRY.payload_model("integrations.disabled")
class IntegrationDisabledV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "integrations.disabled"
    VERSION: ClassVar[int] = 1

    integration_id: UUID
    status: str


@EVENT_REGISTRY.payload_model("integrations.operation.created")
class IntegrationOperationCreatedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "integrations.operation.created"
    VERSION: ClassVar[int] = 1

    integration_id: UUID
    operation_key: str
    operation_type: str
    config_revision: int


@EVENT_REGISTRY.payload_model("integrations.execution.failed")
class IntegrationExecutionFailedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "integrations.execution.failed"
    VERSION: ClassVar[int] = 1

    integration_id: UUID
    operation_key: str
    result_class: str
    error_code: str
    upstream_status: int | None = None
    config_revision: int


@EVENT_REGISTRY.payload_model("integrations.webhook.received")
class IntegrationWebhookReceivedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "integrations.webhook.received"
    VERSION: ClassVar[int] = 1

    webhook_endpoint_id: UUID
    integration_id: UUID
    event_type: str
    external_id: str
