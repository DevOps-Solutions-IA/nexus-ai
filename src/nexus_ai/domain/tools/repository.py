"""Tool Engine persistence (NXS-TOOL-001).

Registry repositories are tenant-scoped by a :class:`TenantSession`. The idempotency
store takes the :class:`Database` and opens its own tenant transactions — it is used from
the invocation path.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.exc import IntegrityError

from nexus_ai.domain.tools.models import (
    ToolDefinitionRecord,
    ToolExecutionRecord,
    ToolIdempotencyRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.tools.entities import (
    RiskClass,
    SideEffectClass,
    ToolBinding,
    ToolBindingType,
    ToolDefinition,
    ToolIdempotencyPolicy,
    ToolStatus,
)
from nexus_ai.tools.errors import ToolConflictError
from nexus_ai.tools.idempotency import (
    ClaimOutcome,
    ToolIdempotencyStatus,
)
from nexus_ai.tools.idempotency import (
    ToolIdempotencyRecord as IdempotencyRow,
)


def _to_definition(row: ToolDefinitionRecord) -> ToolDefinition:
    return ToolDefinition(
        id=row.id,
        organization_id=row.organization_id,
        tool_key=row.tool_key,
        name=row.name,
        description=row.description,
        version=row.version,
        status=ToolStatus(row.status),
        risk_class=RiskClass(row.risk_class),
        side_effect_class=SideEffectClass(row.side_effect_class),
        idempotency_policy=ToolIdempotencyPolicy(row.idempotency_policy),
        timeout_seconds=row.timeout_seconds,
        input_schema=dict(row.input_schema),
        output_schema=None if row.output_schema is None else dict(row.output_schema),
        required_permissions=tuple(row.required_permissions),
        binding=ToolBinding(
            binding_type=ToolBindingType(row.binding_type),
            integration_id=cast("UUID", row.integration_id),
            operation_key=cast("str", row.operation_key),
        ),
        static_arguments=dict(row.static_arguments),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ToolDefinitionRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, tool_id: UUID) -> ToolDefinition | None:
        row = await self._session.get(ToolDefinitionRecord, tool_id)
        return None if row is None else _to_definition(row)

    async def by_key(self, tool_key: str) -> ToolDefinition | None:
        row = (
            (
                await self._session.execute(
                    select(ToolDefinitionRecord).where(
                        ToolDefinitionRecord.organization_id == self._tenant.organization_id,
                        ToolDefinitionRecord.tool_key == tool_key,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_definition(row)

    async def list_all(self, *, limit: int, after_id: UUID | None) -> list[ToolDefinition]:
        query = (
            select(ToolDefinitionRecord)
            .where(ToolDefinitionRecord.organization_id == self._tenant.organization_id)
            .order_by(ToolDefinitionRecord.created_at.desc(), ToolDefinitionRecord.id.desc())
            .limit(limit + 1)
        )
        if after_id is not None:
            query = query.where(ToolDefinitionRecord.id < after_id)
        rows = (await self._session.execute(query)).scalars()
        return [_to_definition(row) for row in rows]

    async def insert(
        self,
        *,
        tool_id: UUID,
        tool_key: str,
        name: str,
        description: str | None,
        risk_class: RiskClass,
        side_effect_class: SideEffectClass,
        idempotency_policy: ToolIdempotencyPolicy,
        timeout_seconds: float | None,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        required_permissions: tuple[str, ...],
        binding: ToolBinding,
        static_arguments: dict[str, Any],
    ) -> ToolDefinition:
        now = dt.datetime.now(dt.UTC)
        record = ToolDefinitionRecord(
            id=tool_id,
            organization_id=self._tenant.organization_id,
            tool_key=tool_key,
            name=name,
            description=description,
            version=1,
            status=ToolStatus.DRAFT.value,
            risk_class=risk_class.value,
            side_effect_class=side_effect_class.value,
            idempotency_policy=idempotency_policy.value,
            timeout_seconds=timeout_seconds,
            input_schema=input_schema,
            output_schema=output_schema,
            required_permissions=list(required_permissions),
            binding_type=binding.binding_type.value,
            integration_id=binding.integration_id,
            operation_key=binding.operation_key,
            static_arguments=static_arguments,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_tool_definitions_org_key" in str(exc.orig):
                raise ToolConflictError(
                    "a tool with that tool_key already exists", cause=exc
                ) from exc
            raise
        await self._session.refresh(record)
        return _to_definition(record)

    async def apply(
        self, tool_id: UUID, *, changes: dict[str, Any], bump_version: bool
    ) -> ToolDefinition | None:
        values = dict(changes)
        values["updated_at"] = dt.datetime.now(dt.UTC)
        if bump_version:
            values["version"] = ToolDefinitionRecord.version + 1
        result = await self._session.execute(
            update(ToolDefinitionRecord)
            .where(
                ToolDefinitionRecord.id == tool_id,
                ToolDefinitionRecord.organization_id == self._tenant.organization_id,
            )
            .values(**values)
            .returning(ToolDefinitionRecord)
        )
        updated = result.scalars().one_or_none()
        return None if updated is None else _to_definition(updated)

    async def delete_one(self, tool_id: UUID) -> bool:
        result = await self._session.execute(
            delete(ToolDefinitionRecord).where(
                ToolDefinitionRecord.id == tool_id,
                ToolDefinitionRecord.organization_id == self._tenant.organization_id,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)


class ToolExecutionRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def record(
        self,
        *,
        tool_id: UUID,
        tool_key: str,
        tool_version: int,
        result_class: str,
        ok: bool,
        status_code: int,
        error_code: str | None,
        downstream_code: str | None,
        downstream_status: int | None,
        retry_count: int,
        duration_ms: int,
        correlation_id: str | None,
        idempotency_key: str | None,
        caller_user_id: UUID | None,
    ) -> None:
        self._session.add(
            ToolExecutionRecord(
                id=uuid.uuid7(),
                organization_id=self._tenant.organization_id,
                tool_id=tool_id,
                tool_key=tool_key,
                tool_version=tool_version,
                result_class=result_class,
                ok=ok,
                status_code=status_code,
                error_code=error_code,
                downstream_code=downstream_code,
                downstream_status=downstream_status,
                retry_count=retry_count,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                caller_user_id=caller_user_id,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await self._session.flush()


class ToolIdempotencyRepository:
    """Satisfies :class:`~nexus_ai.tools.idempotency.ToolIdempotencyStore`. Each method is
    its own tenant transaction; ``claim`` re-resolves the committed winner in a fresh
    transaction on a lost unique-constraint race (P06 pattern)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def claim(
        self,
        *,
        organization_id: UUID,
        tool_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        expires_at: dt.datetime,
    ) -> ClaimOutcome:
        now = dt.datetime.now(dt.UTC)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                tenant.session.add(
                    ToolIdempotencyRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        tool_id=tool_id,
                        tool_key="",
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        status=ToolIdempotencyStatus.PENDING.value,
                        result_json=None,
                        error_code=None,
                        created_at=now,
                        updated_at=now,
                        expires_at=expires_at,
                    )
                )
                await tenant.session.flush()
                return ClaimOutcome(is_owner=True)
        except IntegrityError:
            pass
        existing = await self.get(
            organization_id=organization_id, tool_id=tool_id, idempotency_key=idempotency_key
        )
        return ClaimOutcome(is_owner=False, existing=existing)

    async def finalize(
        self,
        *,
        organization_id: UUID,
        tool_id: UUID,
        idempotency_key: str,
        status: ToolIdempotencyStatus,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await tenant.session.execute(
                update(ToolIdempotencyRecord)
                .where(
                    ToolIdempotencyRecord.organization_id == organization_id,
                    ToolIdempotencyRecord.tool_id == tool_id,
                    ToolIdempotencyRecord.idempotency_key == idempotency_key,
                )
                .values(
                    status=status.value,
                    result_json=result_json,
                    error_code=error_code,
                    updated_at=dt.datetime.now(dt.UTC),
                )
            )

    async def get(
        self,
        *,
        organization_id: UUID,
        tool_id: UUID,
        idempotency_key: str,
    ) -> IdempotencyRow | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(ToolIdempotencyRecord).where(
                            ToolIdempotencyRecord.organization_id == organization_id,
                            ToolIdempotencyRecord.tool_id == tool_id,
                            ToolIdempotencyRecord.idempotency_key == idempotency_key,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if row is None:
                return None
            return IdempotencyRow(
                idempotency_key=row.idempotency_key,
                request_fingerprint=row.request_fingerprint,
                status=ToolIdempotencyStatus(row.status),
                result_json=row.result_json,
                error_code=row.error_code,
                updated_at=row.updated_at,
            )
