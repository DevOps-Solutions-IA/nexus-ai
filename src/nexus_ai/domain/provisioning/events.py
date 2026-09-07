"""Provisioning event payloads (NXS-ORG-001 with NXS-EVENT-009).

Registered in the canonical P04 payload registry: consumers decode through it, and an
unknown type or unsupported version fails closed. No PII, no secrets, no payloads the
tenant supplied verbatim — only identifiers and the trusted server-side outcome.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


@EVENT_REGISTRY.payload_model("organizations.provisioned")
class OrganizationProvisionedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "organizations.provisioned"
    VERSION: ClassVar[int] = 1

    organization_id: UUID
    organization_key: str
    created_by_user_id: UUID
    dashboard_schema_version: int
    dashboard_revision: int
