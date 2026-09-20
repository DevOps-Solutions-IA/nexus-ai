"""New-dialog authorization composes P11 source and P18 placement authority."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from nexus_ai.cells.admission import PlacementAdmission, PlacementResolver
from nexus_ai.domain.sip_edge.models import (
    CellSipTargetHeadRecord,
    CellSipTargetRecord,
    SipRouteAuthorizationRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.contracts import InboundRequest, StrictContract, fingerprint
from nexus_ai.sip_edge.dialogs import DialogResult, record_dialog_result
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.locator import DidLocator, revalidate_source
from nexus_ai.sip_edge.peer_registry import PeerRegistry, require_peer_revision
from nexus_ai.sip_edge.peers import PeerProfile


class InboundAuthorization(StrictContract):
    route_id: UUID
    organization_id: UUID
    cell_id: UUID
    placement_generation: int
    target_id: UUID
    target_revision: int
    target_host: str
    target_port: int
    target_transport: str
    edge_id: UUID
    boot_id: UUID
    issue_deadline: datetime
    state: str


class InboundRoutes:
    """Internal service seam; authenticated peer policy must precede invocation."""

    def __init__(self, database: Database, locator: DidLocator) -> None:
        self._database = database
        self._locator = locator
        self._placement = PlacementResolver(database)

    async def peer_snapshot(self, profile: PeerProfile) -> int:
        return await PeerRegistry(self._database).snapshot(profile)

    async def record_result(
        self,
        organization_id: UUID,
        route_id: UUID,
        edge_id: UUID,
        boot_id: UUID,
        transaction_digest: str,
        result: DialogResult,
    ) -> str:
        return await record_dialog_result(
            self._database, organization_id, route_id, edge_id, boot_id, transaction_digest, result
        )

    async def authorize(
        self, request: InboundRequest, *, edge_id: UUID, boot_id: UUID, peer_revision: int
    ) -> InboundAuthorization:
        request = InboundRequest.model_validate(request.model_dump())
        candidate = await self._locator.discover(request.called_number)
        placement = await self._placement.resolve(candidate.organization_id)
        digest = request.transaction.digest(request.peer_id, "INBOUND")
        semantic = fingerprint(request.model_dump(mode="json"))
        try:
            async with self._database.tenant_transaction(candidate.organization_id) as tenant:
                await PlacementAdmission(placement.cell_id).admit(tenant, placement)
                await revalidate_source(tenant, candidate)
                await require_peer_revision(tenant.session, request.peer_id, peer_revision)
                session = tenant.session
                head = (
                    await session.execute(
                        select(CellSipTargetHeadRecord)
                        .where(CellSipTargetHeadRecord.cell_id == placement.cell_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                existing = (
                    await session.execute(
                        select(SipRouteAuthorizationRecord).where(
                            SipRouteAuthorizationRecord.peer_id == request.peer_id,
                            SipRouteAuthorizationRecord.transaction_digest == digest,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.semantic_digest != semantic or (
                        existing.edge_id,
                        existing.boot_id,
                    ) != (edge_id, boot_id):
                        raise SipConflictError()
                    target = await session.get(CellSipTargetRecord, existing.target_id)
                    if target is None:
                        raise SipRouteDeniedError()
                    return self._view(existing, target)
                if head is None or head.active_target_id is None:
                    raise SipRouteDeniedError()
                target = (
                    await session.execute(
                        select(CellSipTargetRecord)
                        .where(
                            CellSipTargetRecord.id == head.active_target_id,
                            CellSipTargetRecord.cell_id == placement.cell_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if target is None or target.state != "ACTIVE":
                    raise SipRouteDeniedError()
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                route = SipRouteAuthorizationRecord(
                    id=uuid7(),
                    organization_id=candidate.organization_id,
                    peer_id=request.peer_id,
                    phone_number_id=candidate.phone_number_id,
                    account_id=candidate.account_id,
                    e164=candidate.e164,
                    placement_id=placement.placement_id,
                    cell_id=placement.cell_id,
                    placement_generation=placement.assignment_generation,
                    target_id=target.id,
                    target_revision=target.target_revision,
                    transaction_digest=digest,
                    semantic_digest=semantic,
                    edge_id=edge_id,
                    boot_id=boot_id,
                    state="AUTHORIZED",
                    authorized_at=now,
                    issue_deadline=now + timedelta(seconds=5),
                )
                session.add(route)
                await session.flush()
                await session.refresh(route)
                return self._view(route, target)
        except IntegrityError:
            raise SipConflictError() from None
        except DBAPIError:
            raise SipRouteUnavailableError() from None

    async def issue(
        self,
        organization_id: UUID,
        route_id: UUID,
        *,
        edge_id: UUID,
        boot_id: UUID,
        transaction_digest: str,
    ) -> bool:
        """Organization is trusted resolver context, never accepted from an edge header."""
        try:
            return await self._issue(
                organization_id,
                route_id,
                edge_id=edge_id,
                boot_id=boot_id,
                transaction_digest=transaction_digest,
            )
        except IntegrityError:
            raise SipConflictError() from None
        except DBAPIError:
            raise SipRouteUnavailableError() from None

    async def _issue(
        self,
        organization_id: UUID,
        route_id: UUID,
        *,
        edge_id: UUID,
        boot_id: UUID,
        transaction_digest: str,
    ) -> bool:
        async with self._database.tenant_transaction(organization_id) as tenant:
            route = (
                await tenant.session.execute(
                    select(SipRouteAuthorizationRecord)
                    .where(
                        SipRouteAuthorizationRecord.id == route_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if route is None or (route.edge_id, route.boot_id, route.transaction_digest) != (
                edge_id,
                boot_id,
                transaction_digest,
            ):
                raise SipRouteDeniedError()
            if route.state != "AUTHORIZED":
                return False
            now = (await tenant.session.execute(select(func.clock_timestamp()))).scalar_one()
            if now > route.issue_deadline:
                route.state = "EXPIRED"
                await tenant.session.flush()
                return False
            route.state = "ISSUED"
            route.issued_at = now
            await tenant.session.flush()
            return True

    @staticmethod
    def _view(
        route: SipRouteAuthorizationRecord, target: CellSipTargetRecord
    ) -> InboundAuthorization:
        return InboundAuthorization(
            route_id=route.id,
            organization_id=route.organization_id,
            cell_id=route.cell_id,
            placement_generation=route.placement_generation,
            target_id=target.id,
            target_revision=target.target_revision,
            target_host=target.host,
            target_port=target.port,
            target_transport=target.transport,
            edge_id=route.edge_id,
            boot_id=route.boot_id,
            issue_deadline=route.issue_deadline,
            state=route.state,
        )
