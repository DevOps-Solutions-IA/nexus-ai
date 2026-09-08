"""Provider-neutral CRM / ERP / Calendar adapter interfaces (NXS-INT-001, ADR-0054).

These are CONTRACTS, not provider suites. Salesforce / HubSpot / Dynamics / SAP / Oracle /
Odoo / Google Calendar / Microsoft Graph business logic is out of scope for P07 — a
generic REST/OpenAPI adapter that routes every call through the governed Hub path is
sufficient. An adapter can NEVER bypass the destination policy, the credential vault, the
governed executor or the response schemas: every method here ends in
``IntegrationHubService.execute`` with an ``operation_key`` the operator registered.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from nexus_ai.integrations.entities import ExecutionRequest, IntegrationResult
from nexus_ai.integrations.service import IntegrationHubService


class CRMAdapter(Protocol):
    async def get_contact(
        self, organization_id: UUID, integration_id: UUID, contact_id: str
    ) -> IntegrationResult: ...

    async def upsert_contact(
        self, organization_id: UUID, integration_id: UUID, contact: dict[str, Any]
    ) -> IntegrationResult: ...

    async def create_activity(
        self, organization_id: UUID, integration_id: UUID, activity: dict[str, Any]
    ) -> IntegrationResult: ...


class ERPAdapter(Protocol):
    async def get_record(
        self, organization_id: UUID, integration_id: UUID, entity: str, record_id: str
    ) -> IntegrationResult: ...

    async def create_record(
        self, organization_id: UUID, integration_id: UUID, entity: str, payload: dict[str, Any]
    ) -> IntegrationResult: ...


class CalendarAdapter(Protocol):
    async def list_events(
        self, organization_id: UUID, integration_id: UUID, query: dict[str, Any]
    ) -> IntegrationResult: ...

    async def create_event(
        self, organization_id: UUID, integration_id: UUID, event: dict[str, Any]
    ) -> IntegrationResult: ...


class GenericRestAdapter:
    """Satisfies :class:`CRMAdapter`, :class:`ERPAdapter` and :class:`CalendarAdapter`.

    Each method maps to a conventional ``operation_key`` the operator registers on the
    integration (``crm.get_contact``, ``erp.get_record`` …). The adapter adds no transport,
    no auth and no URL — it only shapes the structured input for
    :class:`IntegrationHubService`.
    """

    def __init__(self, service: IntegrationHubService) -> None:
        self._service = service

    async def _invoke(
        self,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        payload: dict[str, Any],
    ) -> IntegrationResult:
        return await self._service.execute(
            organization_id,
            ExecutionRequest(
                integration_id=integration_id,
                operation_key=operation_key,
                input=payload,
            ),
        )

    # -- CRM --
    async def get_contact(
        self, organization_id: UUID, integration_id: UUID, contact_id: str
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id,
            integration_id,
            "crm.get_contact",
            {"path_params": {"id": contact_id}},
        )

    async def upsert_contact(
        self, organization_id: UUID, integration_id: UUID, contact: dict[str, Any]
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id, integration_id, "crm.upsert_contact", {"body": contact}
        )

    async def create_activity(
        self, organization_id: UUID, integration_id: UUID, activity: dict[str, Any]
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id, integration_id, "crm.create_activity", {"body": activity}
        )

    # -- ERP --
    async def get_record(
        self, organization_id: UUID, integration_id: UUID, entity: str, record_id: str
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id,
            integration_id,
            "erp.get_record",
            {"path_params": {"entity": entity, "id": record_id}},
        )

    async def create_record(
        self, organization_id: UUID, integration_id: UUID, entity: str, payload: dict[str, Any]
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id,
            integration_id,
            "erp.create_record",
            {"path_params": {"entity": entity}, "body": payload},
        )

    # -- Calendar --
    async def list_events(
        self, organization_id: UUID, integration_id: UUID, query: dict[str, Any]
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id, integration_id, "calendar.list_events", {"query_params": query}
        )

    async def create_event(
        self, organization_id: UUID, integration_id: UUID, event: dict[str, Any]
    ) -> IntegrationResult:
        return await self._invoke(
            organization_id, integration_id, "calendar.create_event", {"body": event}
        )
