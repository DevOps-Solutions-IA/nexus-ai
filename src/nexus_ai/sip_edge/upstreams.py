"""Explicit immutable carrier revisions controlled by platform operators."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nexus_ai.cells.admission import PlacementAdmission, PlacementResolver
from nexus_ai.domain.sip_edge.models import SipAccountUpstreamRecord, SipUpstreamRecord
from nexus_ai.domain.telephony.models import TelephonyAccountRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.contracts import StrictContract, Transport
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError
from nexus_ai.sip_edge.targets import authorize_control


class RegisterUpstream(StrictContract):
    id: UUID
    revision: Annotated[int, Field(strict=True, ge=1, le=9_223_372_036_854_775_806)]
    host: Annotated[str, StringConstraints(max_length=45)]
    port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    transport: Transport
    cell_id: UUID
    asterisk_peer_id: UUID

    @field_validator("host")
    @classmethod
    def safe_address(cls, value: str) -> str:
        address = ip_address(value)
        if (
            str(address) != value
            or "%" in value
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise ValueError("explicit unicast carrier address required")
        return value


class UpstreamRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def bind_account(
        self,
        actor_user_id: UUID,
        organization_id: UUID,
        account_id: UUID,
        upstream_id: UUID,
        upstream_revision: int,
        *,
        expected_revision: int,
    ) -> int:
        if (
            isinstance(expected_revision, bool)
            or not 0 <= expected_revision < 9_223_372_036_854_775_807
        ):
            raise SipRouteDeniedError()
        placement = await PlacementResolver(self._database).resolve(organization_id)
        try:
            async with self._database.tenant_transaction(organization_id) as tenant:
                await authorize_control(tenant.session, actor_user_id)
                await PlacementAdmission(placement.cell_id).admit(tenant, placement)
                account = (
                    await tenant.session.execute(
                        select(TelephonyAccountRecord)
                        .where(TelephonyAccountRecord.id == account_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                upstream = await tenant.session.get(
                    SipUpstreamRecord, (upstream_id, upstream_revision)
                )
                if (
                    account is None
                    or account.status != "ACTIVE"
                    or upstream is None
                    or upstream.cell_id != placement.cell_id
                ):
                    raise SipRouteDeniedError()
                binding = (
                    await tenant.session.execute(
                        select(SipAccountUpstreamRecord)
                        .where(SipAccountUpstreamRecord.account_id == account_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if binding is None:
                    if expected_revision != 0:
                        raise SipConflictError()
                    binding = SipAccountUpstreamRecord(
                        organization_id=organization_id,
                        account_id=account_id,
                        upstream_id=upstream_id,
                        upstream_revision=upstream_revision,
                        revision=1,
                    )
                    tenant.session.add(binding)
                elif binding.revision == expected_revision + 1 and (
                    binding.upstream_id,
                    binding.upstream_revision,
                ) == (upstream_id, upstream_revision):
                    return binding.revision
                elif binding.revision != expected_revision:
                    raise SipConflictError()
                else:
                    binding.upstream_id, binding.upstream_revision = upstream_id, upstream_revision
                    binding.revision += 1
                await tenant.session.flush()
                return binding.revision
        except IntegrityError:
            raise SipConflictError() from None

    async def register(self, actor_user_id: UUID, request: RegisterUpstream) -> None:
        request = RegisterUpstream.model_validate(request.model_dump())
        values = request.model_dump()
        values["transport"] = request.transport.value
        try:
            async with self._database.transaction() as session:
                await authorize_control(session, actor_user_id)
                existing = await session.get(SipUpstreamRecord, (request.id, request.revision))
                if existing is not None:
                    if any(getattr(existing, name) != value for name, value in values.items()):
                        raise SipConflictError()
                    return
                session.add(SipUpstreamRecord(**values))
                await session.flush()
        except IntegrityError:
            raise SipConflictError() from None
