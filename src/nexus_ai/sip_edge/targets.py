"""Platform-authorized target registration with durable revision and retry identity."""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from uuid import UUID, uuid7

from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.domain.auth.models import UserRecord
from nexus_ai.domain.cells.models import CellRecord
from nexus_ai.domain.provisioning.models import PlatformGrantRecord
from nexus_ai.domain.sip_edge.models import (
    CellSipTargetHeadRecord,
    CellSipTargetRecord,
    SipTargetMutationRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.contracts import Page, RegisterTarget, StrictContract, fingerprint
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError


class TargetResult(StrictContract):
    target_id: UUID
    cell_id: UUID
    control_revision: int = Field(ge=1)
    state: str


class TargetView(StrictContract):
    target_id: UUID
    cell_id: UUID
    target_revision: int = Field(ge=1)
    host: str
    port: int
    transport: str
    state: str


class TargetNetworkPolicy:
    def __init__(self, networks: tuple[str, ...], ports: frozenset[int]) -> None:
        if not 1 <= len(networks) <= 64 or not 1 <= len(ports) <= 32:
            raise ValueError("bounded target networks and ports are required")
        parsed = tuple(ip_network(network, strict=True) for network in networks)
        private = tuple(
            ip_network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        )
        for network in parsed:
            if not any(
                network.version == allowed.version
                and int(network.network_address) >= int(allowed.network_address)
                and int(network.broadcast_address) <= int(allowed.broadcast_address)
                for allowed in private
            ):
                raise ValueError("target network must be private and bounded")
        if any(isinstance(port, bool) or not 1024 <= port <= 65535 for port in ports):
            raise ValueError("invalid target port")
        self._networks: tuple[IPv4Network | IPv6Network, ...] = parsed
        self._ports = ports

    def require(self, request: RegisterTarget) -> None:
        address = ip_address(request.host)
        if request.port not in self._ports or not any(
            address in network for network in self._networks
        ):
            raise SipRouteDeniedError()


async def authorize_control(session: AsyncSession, actor_user_id: UUID) -> None:
    grant = (
        await session.execute(
            select(PlatformGrantRecord.user_id)
            .join(UserRecord, UserRecord.id == PlatformGrantRecord.user_id)
            .where(
                PlatformGrantRecord.user_id == actor_user_id,
                PlatformGrantRecord.capability == "sip_edge:control",
                UserRecord.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise PermissionDeniedError()
    await session.execute(
        text("SELECT set_config('nxs.principal_id', :actor, true)"), {"actor": str(actor_user_id)}
    )


class TargetRegistry:
    def __init__(self, database: Database, network_policy: TargetNetworkPolicy) -> None:
        self._database = database
        self._network_policy = network_policy

    async def register(
        self, actor_user_id: UUID, request: RegisterTarget, correlation_id: UUID
    ) -> TargetResult:
        try:
            return await self._register(actor_user_id, request, correlation_id)
        except IntegrityError:
            raise SipConflictError() from None

    async def _register(
        self, actor_user_id: UUID, request: RegisterTarget, correlation_id: UUID
    ) -> TargetResult:
        request = RegisterTarget.model_validate(request.model_dump())
        self._network_policy.require(request)
        key_hash = fingerprint({"key": request.idempotency_key})
        semantic = fingerprint(request.model_dump(mode="json", exclude={"idempotency_key"}))
        async with self._database.transaction() as session:
            await authorize_control(session, actor_user_id)
            cell = (
                await session.execute(
                    select(CellRecord).where(CellRecord.id == request.cell_id).with_for_update()
                )
            ).scalar_one_or_none()
            if cell is None or cell.state != "REGISTERED":
                raise SipRouteDeniedError()
            existing = (
                await session.execute(
                    select(SipTargetMutationRecord).where(
                        SipTargetMutationRecord.operation == "REGISTER",
                        SipTargetMutationRecord.key_hash == key_hash,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.fingerprint != semantic:
                    raise SipConflictError()
                return TargetResult(
                    target_id=existing.target_id,
                    cell_id=existing.cell_id,
                    control_revision=existing.result_revision,
                    state=existing.result_state,
                )
            head = await session.get(CellSipTargetHeadRecord, request.cell_id)
            if head is None:
                head = CellSipTargetHeadRecord(cell_id=request.cell_id, control_revision=0)
                session.add(head)
            if head.control_revision != request.expected_revision:
                raise SipConflictError()
            revision = head.control_revision + 1
            target_id = uuid7()
            session.add(
                CellSipTargetRecord(
                    id=target_id,
                    cell_id=request.cell_id,
                    target_revision=revision,
                    host=request.host,
                    port=request.port,
                    transport=request.transport.value,
                    state="REGISTERED",
                )
            )
            await session.flush()
            head.control_revision = revision
            session.add(
                SipTargetMutationRecord(
                    id=uuid7(),
                    cell_id=request.cell_id,
                    target_id=target_id,
                    operation="REGISTER",
                    key_hash=key_hash,
                    fingerprint=semantic,
                    expected_revision=request.expected_revision,
                    result_revision=revision,
                    result_state="REGISTERED",
                    actor_user_id=actor_user_id,
                    reason_code=request.reason_code,
                    correlation_id=correlation_id,
                )
            )
            await session.flush()
            return TargetResult(
                target_id=target_id,
                cell_id=request.cell_id,
                control_revision=revision,
                state="REGISTERED",
            )

    async def list_targets(
        self, actor_user_id: UUID, cell_id: UUID, page: Page
    ) -> list[TargetView]:
        page = Page.model_validate(page.model_dump())
        async with self._database.transaction() as session:
            await authorize_control(session, actor_user_id)
            query = select(CellSipTargetRecord).where(CellSipTargetRecord.cell_id == cell_id)
            if page.after_id is not None:
                query = query.where(CellSipTargetRecord.id > page.after_id)
            rows = (
                await session.execute(query.order_by(CellSipTargetRecord.id).limit(page.limit))
            ).scalars()
            return [
                TargetView(
                    target_id=row.id,
                    cell_id=row.cell_id,
                    target_revision=row.target_revision,
                    host=row.host,
                    port=row.port,
                    transport=row.transport,
                    state=row.state,
                )
                for row in rows
            ]
