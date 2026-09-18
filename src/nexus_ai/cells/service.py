"""Explicit platform-control placement operations with atomic tenant event intent."""

from __future__ import annotations

from uuid import UUID, uuid7

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.cells import events
from nexus_ai.cells.admission import snapshot
from nexus_ai.cells.contracts import (
    CellView,
    PlacementMutation,
    PlacementOperation,
    PlacementPage,
    PlacementSnapshot,
    PlacementState,
    RegisterCellRequest,
    RetireCellRequest,
    semantic_fingerprint,
)
from nexus_ai.cells.errors import (
    CellUnavailableError,
    PlacementConflictError,
    PlacementRequiredError,
)
from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.domain.auth.models import UserRecord
from nexus_ai.domain.cells.models import (
    CellControlHistoryRecord,
    CellRecord,
    OrganizationPlacementRecord,
    PlacementMutationRecord,
)
from nexus_ai.domain.organizations.models import OrganizationRecord
from nexus_ai.domain.provisioning.models import PlatformGrantRecord
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database


class CellPlacementService:
    def __init__(self, database: Database, publisher: EventPublisher) -> None:
        self._database = database
        self._publisher = publisher

    async def _authorize(self, session: AsyncSession, actor_user_id: UUID) -> None:
        grant = (
            await session.execute(
                select(PlatformGrantRecord.user_id)
                .join(UserRecord, UserRecord.id == PlatformGrantRecord.user_id)
                .where(
                    PlatformGrantRecord.user_id == actor_user_id,
                    PlatformGrantRecord.capability == "cell:control",
                    UserRecord.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            raise PermissionDeniedError()
        await session.execute(
            text("SELECT set_config('nxs.principal_id', :actor, true)"),
            {"actor": str(actor_user_id)},
        )

    async def register(
        self, actor_user_id: UUID, request: RegisterCellRequest, correlation_id: UUID
    ) -> UUID:
        request = RegisterCellRequest.model_validate(request.model_dump())
        key_hash = semantic_fingerprint({"key": request.idempotency_key})
        fingerprint = semantic_fingerprint(request.model_dump(exclude={"idempotency_key"}))
        async with self._database.transaction() as session:
            await self._authorize(session, actor_user_id)
            existing = (
                await session.execute(
                    select(CellRecord).where(CellRecord.registration_key_hash == key_hash)
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.registration_fingerprint != fingerprint:
                    raise PlacementConflictError()
                return existing.id
            cell_id = uuid7()
            try:
                async with session.begin_nested():
                    session.add(
                        CellRecord(
                            id=cell_id,
                            cell_key=request.cell_key,
                            registration_key_hash=key_hash,
                            registration_fingerprint=fingerprint,
                            state="REGISTERED",
                        )
                    )
                    await session.flush()
            except IntegrityError:
                winner = (
                    await session.execute(
                        select(CellRecord).where(CellRecord.registration_key_hash == key_hash)
                    )
                ).scalar_one_or_none()
                if winner is None or winner.registration_fingerprint != fingerprint:
                    raise PlacementConflictError() from None
                return winner.id
            session.add(
                CellControlHistoryRecord(
                    id=uuid7(),
                    cell_id=cell_id,
                    actor_user_id=actor_user_id,
                    operation="REGISTER",
                    idempotency_key_hash=key_hash,
                    fingerprint=fingerprint,
                    reason_code=request.reason_code,
                    correlation_id=correlation_id,
                )
            )
            return cell_id

    async def inspect_cell(self, actor_user_id: UUID, cell_id: UUID) -> CellView:
        async with self._database.transaction() as session:
            await self._authorize(session, actor_user_id)
            cell = await session.get(CellRecord, cell_id)
            if cell is None:
                raise CellUnavailableError()
            return CellView(id=cell.id, cell_key=cell.cell_key, state=cell.state)

    async def inspect_placement(
        self, organization_id: UUID, actor_user_id: UUID
    ) -> PlacementSnapshot:
        async with self._database.tenant_transaction(organization_id) as tenant:
            await self._authorize(tenant.session, actor_user_id)
            row = (
                await tenant.session.execute(
                    select(OrganizationPlacementRecord).where(
                        OrganizationPlacementRecord.organization_id == organization_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise PlacementRequiredError()
            return snapshot(row)

    async def list_cells(self, actor_user_id: UUID, page: PlacementPage) -> list[CellView]:
        page = PlacementPage.model_validate(page.model_dump())
        async with self._database.transaction() as session:
            await self._authorize(session, actor_user_id)
            statement = select(CellRecord).order_by(CellRecord.id).limit(page.limit)
            if page.after_id is not None:
                statement = statement.where(CellRecord.id > page.after_id)
            rows = (await session.execute(statement)).scalars().all()
            return [CellView(id=row.id, cell_key=row.cell_key, state=row.state) for row in rows]

    async def retire(
        self, actor_user_id: UUID, request: RetireCellRequest, correlation_id: UUID
    ) -> CellView:
        request = RetireCellRequest.model_validate(request.model_dump())
        key_hash = semantic_fingerprint({"key": request.idempotency_key})
        fingerprint = semantic_fingerprint(
            request.model_dump(mode="json", exclude={"idempotency_key"})
        )
        async with self._database.transaction() as session:
            await self._authorize(session, actor_user_id)
            cell = (
                await session.execute(
                    select(CellRecord).where(CellRecord.id == request.cell_id).with_for_update()
                )
            ).scalar_one_or_none()
            if cell is None:
                raise CellUnavailableError()
            receipt = (
                await session.execute(
                    select(CellControlHistoryRecord).where(
                        CellControlHistoryRecord.operation == "RETIRE",
                        CellControlHistoryRecord.idempotency_key_hash == key_hash,
                    )
                )
            ).scalar_one_or_none()
            if receipt is not None:
                if receipt.fingerprint != fingerprint:
                    raise PlacementConflictError()
                return CellView(id=cell.id, cell_key=cell.cell_key, state=cell.state)
            if cell.state != "REGISTERED" or cell.has_placements:
                raise PlacementConflictError()
            cell.state = "RETIRED"
            cell.updated_at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            session.add(
                CellControlHistoryRecord(
                    id=uuid7(),
                    cell_id=cell.id,
                    actor_user_id=actor_user_id,
                    operation="RETIRE",
                    idempotency_key_hash=key_hash,
                    fingerprint=fingerprint,
                    reason_code=request.reason_code,
                    correlation_id=correlation_id,
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                raise PlacementConflictError() from None
            return CellView(id=cell.id, cell_key=cell.cell_key, state=cell.state)

    async def mutate(
        self,
        organization_id: UUID,
        actor_user_id: UUID,
        request: PlacementMutation,
        correlation_id: UUID,
    ) -> PlacementSnapshot:
        request = PlacementMutation.model_validate(request.model_dump())
        async with self._database.tenant_transaction(organization_id) as tenant:
            session = tenant.session
            await self._authorize(session, actor_user_id)
            key_hash = semantic_fingerprint({"key": request.idempotency_key})
            prior_fingerprint = (
                await session.execute(
                    select(PlacementMutationRecord.fingerprint).where(
                        PlacementMutationRecord.organization_id == organization_id,
                        PlacementMutationRecord.operation == request.operation,
                        PlacementMutationRecord.idempotency_key_hash == key_hash,
                    )
                )
            ).scalar_one_or_none()
            if prior_fingerprint is not None and prior_fingerprint != request.fingerprint():
                raise PlacementConflictError()
            cell = (
                await session.execute(
                    select(CellRecord).where(CellRecord.id == request.cell_id).with_for_update()
                )
            ).scalar_one_or_none()
            if cell is None or cell.state != "REGISTERED":
                raise CellUnavailableError()
            organization = (
                await session.execute(
                    select(OrganizationRecord.id)
                    .where(OrganizationRecord.id == organization_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if organization is None:
                raise PermissionDeniedError()
            receipt = (
                await session.execute(
                    select(PlacementMutationRecord).where(
                        PlacementMutationRecord.organization_id == organization_id,
                        PlacementMutationRecord.operation == request.operation,
                        PlacementMutationRecord.idempotency_key_hash == key_hash,
                    )
                )
            ).scalar_one_or_none()
            if receipt is not None:
                if receipt.fingerprint != request.fingerprint():
                    raise PlacementConflictError()
                return PlacementSnapshot(
                    organization_id=organization_id,
                    placement_id=receipt.placement_id,
                    cell_id=receipt.cell_id,
                    assignment_generation=receipt.result_generation,
                    state=PlacementState(receipt.new_state),
                )
            placement = (
                await session.execute(
                    select(OrganizationPlacementRecord)
                    .where(OrganizationPlacementRecord.organization_id == organization_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            previous_state = None if placement is None else placement.state
            if request.operation is PlacementOperation.ASSIGN:
                if placement is not None:
                    raise PlacementConflictError()
                placement = OrganizationPlacementRecord(
                    id=uuid7(),
                    organization_id=organization_id,
                    cell_id=request.cell_id,
                    assignment_generation=1,
                    state="ACTIVE",
                )
                session.add(placement)
                event_type = events.AssignmentCreated.EVENT_TYPE
            else:
                required_state = (
                    "ACTIVE" if request.operation is PlacementOperation.SUSPEND else "SUSPENDED"
                )
                if (
                    placement is None
                    or placement.cell_id != request.cell_id
                    or placement.assignment_generation != request.expected_generation
                    or placement.assignment_generation == 9_223_372_036_854_775_807
                    or placement.state != required_state
                ):
                    raise PlacementConflictError()
                placement.state = "SUSPENDED" if required_state == "ACTIVE" else "ACTIVE"
                placement.assignment_generation += 1
                placement.updated_at = (
                    await session.execute(select(func.clock_timestamp()))
                ).scalar_one()
                event_type = (
                    events.AssignmentSuspended.EVENT_TYPE
                    if placement.state == "SUSPENDED"
                    else events.AssignmentResumed.EVENT_TYPE
                )
            await session.flush()
            session.add(
                PlacementMutationRecord(
                    id=uuid7(),
                    organization_id=organization_id,
                    placement_id=placement.id,
                    cell_id=placement.cell_id,
                    operation=request.operation,
                    idempotency_key_hash=key_hash,
                    fingerprint=request.fingerprint(),
                    expected_generation=request.expected_generation,
                    result_generation=placement.assignment_generation,
                    previous_state=previous_state,
                    new_state=placement.state,
                    actor_user_id=actor_user_id,
                    reason_code=request.reason_code,
                    correlation_id=correlation_id,
                )
            )
            await self._publisher.enqueue(
                session,
                EventEnvelope.create(
                    event_type=event_type,
                    event_version=1,
                    aggregate_type="cell_assignment",
                    aggregate_id=str(placement.id),
                    producer="nexus-ai",
                    organization_id=organization_id,
                    correlation_id=str(correlation_id),
                    payload={
                        "placement_id": str(placement.id),
                        "cell_id": str(placement.cell_id),
                        "assignment_generation": placement.assignment_generation,
                        "state": placement.state,
                        "reason_code": request.reason_code,
                    },
                ),
            )
            return snapshot(placement)
