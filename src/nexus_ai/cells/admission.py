"""Shared-lock placement admission inside the protected domain transaction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from nexus_ai.cells.contracts import PlacementSnapshot, PlacementState
from nexus_ai.cells.errors import CellUnavailableError, PlacementFencedError, PlacementRequiredError
from nexus_ai.core.errors import ConfigurationError, DependencyUnavailableError
from nexus_ai.domain.cells.models import CellRecord, OrganizationPlacementRecord
from nexus_ai.infrastructure.tenant_session import TenantSession

if TYPE_CHECKING:
    from nexus_ai.infrastructure.database import Database

_route: ContextVar[PlacementSnapshot | None] = ContextVar("placement_route", default=None)


@contextmanager
def placement_route(expected: PlacementSnapshot) -> Iterator[None]:
    """Bind a routing snapshot, not authority, for internal execution delivery.

    Every use still validates server Cell configuration and PostgreSQL state.
    No HTTP header or body is wired to this context.
    """
    expected = PlacementSnapshot.model_validate(expected.model_dump())
    token = _route.set(expected)
    try:
        yield
    finally:
        _route.reset(token)


def current_placement_route() -> PlacementSnapshot | None:
    return _route.get()


def snapshot(row: OrganizationPlacementRecord) -> PlacementSnapshot:
    return PlacementSnapshot(
        organization_id=row.organization_id,
        placement_id=row.id,
        cell_id=row.cell_id,
        assignment_generation=row.assignment_generation,
        state=PlacementState(row.state),
    )


class PlacementResolver:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve(self, organization_id: UUID) -> PlacementSnapshot:
        try:
            return await self._resolve(organization_id)
        except DBAPIError, ConfigurationError:
            raise DependencyUnavailableError("Placement authority is unavailable.") from None

    async def _resolve(self, organization_id: UUID) -> PlacementSnapshot:
        async with self._database.tenant_transaction(organization_id) as tenant:
            result = (
                await tenant.session.execute(
                    select(OrganizationPlacementRecord, CellRecord.state)
                    .join(CellRecord, CellRecord.id == OrganizationPlacementRecord.cell_id)
                    .where(OrganizationPlacementRecord.organization_id == organization_id)
                )
            ).one_or_none()
            if result is None:
                raise PlacementRequiredError()
            placement, cell_state = result
            if cell_state != "REGISTERED":
                raise CellUnavailableError()
            return snapshot(placement)


class PlacementAdmission:
    """Construct only from trusted worker configuration, never request headers.

    Call before acquiring any domain lock. Locks remain held by the caller's
    transaction. This object stores no durable authority and performs no I/O
    outside PostgreSQL.
    """

    def __init__(self, worker_cell_id: UUID) -> None:
        if not isinstance(worker_cell_id, UUID):
            raise PlacementFencedError()
        self._worker_cell_id = worker_cell_id

    async def admit(self, tenant: TenantSession, expected: PlacementSnapshot) -> None:
        if (
            expected.organization_id != tenant.organization_id
            or expected.cell_id != self._worker_cell_id
        ):
            raise PlacementFencedError()
        cell = (
            await tenant.session.execute(
                select(CellRecord)
                .where(CellRecord.id == self._worker_cell_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if cell is None or cell.state != "REGISTERED":
            raise CellUnavailableError()
        placement = (
            await tenant.session.execute(
                select(OrganizationPlacementRecord)
                .where(OrganizationPlacementRecord.organization_id == tenant.organization_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if placement is None:
            raise PlacementRequiredError()
        if (
            placement.id != expected.placement_id
            or placement.cell_id != self._worker_cell_id
            or placement.assignment_generation != expected.assignment_generation
            or placement.state != PlacementState.ACTIVE
            or expected.state != PlacementState.ACTIVE
        ):
            raise PlacementFencedError()
