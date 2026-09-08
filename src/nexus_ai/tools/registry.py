"""The Tool Registry (NXS-TOOL-001, ADR-0061).

Tenant-owned CRUD for deterministic, versioned :class:`ToolDefinition` rows. Every tool
binds to exactly one registered Integration Hub operation; the binding is validated here
(the integration exists in this Organization and declares the operation) and again at
invocation. Any change to a schema, a binding, the side-effect class, the risk class or
the permission set increments ``version`` so an invocation can be attributed to the exact
definition it ran against.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.tools.repository import ToolDefinitionRepository
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.registry import IntegrationRegistry
from nexus_ai.tools.entities import (
    RegisterToolRequest,
    RiskClass,
    ToolBinding,
    ToolDefinition,
    ToolStatus,
    UpdateToolRequest,
)
from nexus_ai.tools.errors import (
    ToolBindingInvalidError,
    ToolConfigInvalidError,
    ToolNotFoundError,
    ToolPolicyDeniedError,
)
from nexus_ai.tools.permissions import normalise_required_permissions
from nexus_ai.tools.schemas import validate_schema_document

_RISK_ORDER = {RiskClass.LOW: 0, RiskClass.MEDIUM: 1, RiskClass.HIGH: 2, RiskClass.CRITICAL: 3}
_ACTIVATABLE = {ToolStatus.ACTIVE, ToolStatus.DISABLED}


class ToolRegistry:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        integrations: IntegrationRegistry,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._integrations = integrations
        self._log = get_logger("nexus_ai.tools.registry")

    # -- tools ---------------------------------------------------------------

    async def register(self, organization_id: UUID, request: RegisterToolRequest) -> ToolDefinition:
        validate_schema_document(request.input_schema, closed_object=True)
        if request.output_schema is not None:
            validate_schema_document(request.output_schema, closed_object=False)
        self._check_risk_ceiling(request.risk_class)
        required = normalise_required_permissions(request.required_permissions)
        await self._validate_binding(organization_id, request.binding)

        async with self._db.tenant_transaction(organization_id) as tenant:
            tool = await ToolDefinitionRepository(tenant).insert(
                tool_id=uuid.uuid7(),
                tool_key=request.tool_key,
                name=request.name,
                description=request.description,
                risk_class=request.risk_class,
                side_effect_class=request.side_effect_class,
                idempotency_policy=request.idempotency_policy,
                timeout_seconds=request.timeout_seconds,
                input_schema=request.input_schema,
                output_schema=request.output_schema,
                required_permissions=required,
                binding=request.binding,
                static_arguments={},
            )
            await self._emit(
                tenant.session,
                organization_id,
                "tools.registered",
                tool.id,
                {
                    "tool_id": str(tool.id),
                    "tool_key": tool.tool_key,
                    "risk_class": tool.risk_class.value,
                    "side_effect_class": tool.side_effect_class.value,
                },
            )
            return tool

    async def get(self, organization_id: UUID, tool_id: UUID) -> ToolDefinition:
        async with self._db.tenant_transaction(organization_id) as tenant:
            tool = await ToolDefinitionRepository(tenant).by_id(tool_id)
        if tool is None:
            raise ToolNotFoundError("no such tool in this Organization")
        return tool

    async def get_by_key(self, organization_id: UUID, tool_key: str) -> ToolDefinition:
        async with self._db.tenant_transaction(organization_id) as tenant:
            tool = await ToolDefinitionRepository(tenant).by_key(tool_key)
        if tool is None:
            raise ToolNotFoundError("no such tool in this Organization")
        return tool

    async def list_tools(
        self, organization_id: UUID, *, limit: int, after_id: UUID | None
    ) -> list[ToolDefinition]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            rows = await ToolDefinitionRepository(tenant).list_all(limit=limit, after_id=after_id)
        return rows[:limit]

    async def update(
        self, organization_id: UUID, tool_id: UUID, request: UpdateToolRequest
    ) -> ToolDefinition:
        current = await self.get(organization_id, tool_id)
        changes: dict[str, Any] = {}
        bump = False
        if request.name is not None:
            changes["name"] = request.name
        if request.description is not None:
            changes["description"] = request.description
        if request.risk_class is not None and request.risk_class is not current.risk_class:
            self._check_risk_ceiling(request.risk_class)
            changes["risk_class"] = request.risk_class.value
            bump = True
        if request.side_effect_class is not None:
            changes["side_effect_class"] = request.side_effect_class.value
            bump = True
        if request.idempotency_policy is not None:
            changes["idempotency_policy"] = request.idempotency_policy.value
            bump = True
        if request.timeout_seconds is not None:
            changes["timeout_seconds"] = request.timeout_seconds
        if request.input_schema is not None:
            validate_schema_document(request.input_schema, closed_object=True)
            changes["input_schema"] = request.input_schema
            bump = True
        if request.output_schema is not None:
            validate_schema_document(request.output_schema, closed_object=False)
            changes["output_schema"] = request.output_schema
            bump = True
        if request.required_permissions is not None:
            changes["required_permissions"] = list(
                normalise_required_permissions(request.required_permissions)
            )
            bump = True
        if request.binding is not None:
            await self._validate_binding(organization_id, request.binding)
            changes["binding_type"] = request.binding.binding_type.value
            changes["integration_id"] = request.binding.integration_id
            changes["operation_key"] = request.binding.operation_key
            bump = True
        # A side-effecting tool must always allow an idempotency key.
        effective_side_effect = request.side_effect_class or current.side_effect_class
        effective_policy = request.idempotency_policy or current.idempotency_policy
        from nexus_ai.tools.entities import SIDE_EFFECTING, ToolIdempotencyPolicy

        if (
            effective_side_effect in SIDE_EFFECTING
            and effective_policy is ToolIdempotencyPolicy.NONE
        ):
            raise ToolConfigInvalidError(
                "a NON_IDEMPOTENT_WRITE / EXTERNAL_EFFECT tool must allow an idempotency key"
            )
        if not changes:
            return current
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await ToolDefinitionRepository(tenant).apply(
                tool_id, changes=changes, bump_version=bump
            )
            if updated is None:  # pragma: no cover - get() already proved existence
                raise ToolNotFoundError("no such tool in this Organization")
            if bump:
                await self._emit(
                    tenant.session,
                    organization_id,
                    "tools.updated",
                    tool_id,
                    {
                        "tool_id": str(tool_id),
                        "tool_key": updated.tool_key,
                        "version": updated.version,
                    },
                )
            return updated

    async def set_status(
        self, organization_id: UUID, tool_id: UUID, status: ToolStatus
    ) -> ToolDefinition:
        if status not in _ACTIVATABLE:
            raise ToolConfigInvalidError("status may only be set to ACTIVE or DISABLED")
        current = await self.get(organization_id, tool_id)
        if status is ToolStatus.ACTIVE:
            self._check_risk_ceiling(current.risk_class)
            await self._validate_binding(organization_id, current.binding)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await ToolDefinitionRepository(tenant).apply(
                tool_id, changes={"status": status.value}, bump_version=False
            )
            assert updated is not None  # noqa: S101 - get() proved existence
            if status is ToolStatus.DISABLED:
                await self._emit(
                    tenant.session,
                    organization_id,
                    "tools.disabled",
                    tool_id,
                    {
                        "tool_id": str(tool_id),
                        "tool_key": updated.tool_key,
                        "status": status.value,
                    },
                )
            return updated

    async def remove(self, organization_id: UUID, tool_id: UUID) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            removed = await ToolDefinitionRepository(tenant).delete_one(tool_id)
        if not removed:
            raise ToolNotFoundError("no such tool in this Organization")

    async def resolve_for_invocation(self, organization_id: UUID, tool_key: str) -> ToolDefinition:
        tool = await self.get_by_key(organization_id, tool_key)
        return tool

    # -- helpers ---------------------------------------------------------

    def _check_risk_ceiling(self, risk_class: RiskClass) -> None:
        ceiling = RiskClass(self._settings.tools.max_risk_class)
        if _RISK_ORDER[risk_class] > _RISK_ORDER[ceiling]:
            raise ToolPolicyDeniedError(
                f"risk class {risk_class} exceeds this Organization's ceiling {ceiling}",
                extensions={"risk_class": risk_class.value, "ceiling": ceiling.value},
            )

    async def _validate_binding(self, organization_id: UUID, binding: ToolBinding) -> None:
        from nexus_ai.integrations.errors import IntegrationNotFoundError

        try:
            integration = await self._integrations.get(organization_id, binding.integration_id)
            operations = await self._integrations.list_operations(organization_id, integration.id)
        except IntegrationNotFoundError as exc:
            raise ToolBindingInvalidError(
                "the bound integration does not exist in this Organization", cause=exc
            ) from exc
        if binding.operation_key not in {op.operation_key for op in operations}:
            raise ToolBindingInvalidError(
                "the bound integration does not declare this operation_key",
                extensions={"operation_key": binding.operation_key},
            )

    async def _emit(
        self,
        session: Any,
        organization_id: UUID,
        event_type: str,
        tool_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        ctx = current_context()
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="tool",
            aggregate_id=str(tool_id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)
