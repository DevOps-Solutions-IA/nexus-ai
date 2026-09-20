"""One logical P11 call, one PostgreSQL-fenced outbound authorization."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID, uuid7

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from nexus_ai.cells.admission import PlacementAdmission, PlacementResolver
from nexus_ai.domain.organizations.models import OrganizationRecord
from nexus_ai.domain.sip_edge.models import (
    SipAccountUpstreamRecord,
    SipEgressDialogBindingRecord,
    SipEgressPermitRecord,
    SipUpstreamRecord,
)
from nexus_ai.domain.telephony.models import TelephonyAccountRecord, TelephonyCallRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.sip_edge.contracts import E164, SipTransaction, StrictContract, fingerprint
from nexus_ai.sip_edge.dialogs import DialogResult
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.peer_registry import PeerRegistry, require_peer_revision
from nexus_ai.sip_edge.peers import PeerObservation, PeerPolicy


class PermitToken(StrictContract):
    organization_id: UUID
    permit_id: UUID
    entropy: str


class EgressDecision(StrictContract):
    organization_id: UUID
    permit_id: UUID
    initial_relay_granted: bool
    host: str
    port: int
    transport: str
    upstream_id: UUID
    upstream_revision: int


async def revalidate_call(
    tenant: TenantSession, call_id: UUID, account_id: UUID
) -> TelephonyCallRecord:
    organization = (
        await tenant.session.execute(
            select(OrganizationRecord)
            .where(
                OrganizationRecord.id == tenant.organization_id,
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    account = (
        await tenant.session.execute(
            select(TelephonyAccountRecord)
            .where(
                TelephonyAccountRecord.id == account_id,
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    call = (
        await tenant.session.execute(
            select(TelephonyCallRecord)
            .where(
                TelephonyCallRecord.id == call_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        organization is None
        or organization.status != "ACTIVE"
        or account is None
        or account.status != "ACTIVE"
        or call is None
        or call.account_id != account_id
        or call.direction != "OUTBOUND"
        or call.state not in {"CREATED", "RINGING", "ANSWERED", "BRIDGED"}
    ):
        raise SipRouteDeniedError()
    return call


class EgressPermits:
    def __init__(self, database: Database, token_key: bytes, peers: PeerPolicy) -> None:
        self._database = database
        self._cipher = Fernet(token_key)
        self._peers = peers
        self._placement = PlacementResolver(database)

    async def record_result(
        self,
        organization_id: UUID,
        permit_id: UUID,
        edge_id: UUID,
        boot_id: UUID,
        transaction_digest: str,
        result: DialogResult,
    ) -> str:
        result = DialogResult.model_validate(result.model_dump())
        if result.state in {"ESTABLISHED", "ENDED"} and result.to_tag is None:
            raise SipRouteDeniedError()
        digest = fingerprint({"call": result.call_id, "from": result.from_tag, "to": result.to_tag})
        try:
            async with self._database.tenant_transaction(organization_id) as tenant:
                permit = (
                    await tenant.session.execute(
                        select(SipEgressPermitRecord)
                        .where(SipEgressPermitRecord.id == permit_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if permit is None or (
                    permit.edge_id,
                    permit.boot_id,
                    permit.transaction_digest,
                ) != (
                    edge_id,
                    boot_id,
                    transaction_digest,
                ):
                    raise SipRouteDeniedError()
                binding = await tenant.session.get(SipEgressDialogBindingRecord, permit_id)
                if result.state in {"ESTABLISHED", "ENDED"}:
                    if binding is not None and binding.dialog_digest != digest:
                        raise SipConflictError()
                    if binding is None:
                        if result.state != "ESTABLISHED" or permit.state != "CONSUMED":
                            raise SipConflictError()
                        tenant.session.add(
                            SipEgressDialogBindingRecord(
                                organization_id=organization_id,
                                permit_id=permit_id,
                                dialog_digest=digest,
                            )
                        )
                        await tenant.session.flush()
                if result.state == "ESTABLISHED":
                    if permit.state != "CONSUMED":
                        raise SipConflictError()
                    return "ESTABLISHED"
                target_state = "ENDED" if result.state == "FAILED" else result.state
                if result.state == "FAILED" and binding is not None:
                    raise SipConflictError()
                if permit.state == target_state:
                    return permit.state
                if permit.state != "CONSUMED":
                    raise SipConflictError()
                permit.state = target_state
                await tenant.session.flush()
                return permit.state
        except IntegrityError:
            raise SipConflictError() from None
        except DBAPIError:
            raise SipRouteUnavailableError() from None

    @staticmethod
    async def _binding(
        tenant: TenantSession, account_id: UUID, upstream_id: UUID, revision: int
    ) -> None:
        binding = (
            await tenant.session.execute(
                select(SipAccountUpstreamRecord)
                .where(SipAccountUpstreamRecord.account_id == account_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if binding is None or (binding.upstream_id, binding.upstream_revision) != (
            upstream_id,
            revision,
        ):
            raise SipRouteDeniedError()

    async def issue_for_call(
        self, organization_id: UUID, call_id: UUID, account_id: UUID
    ) -> SecretStr:
        async with self._database.tenant_transaction(organization_id) as tenant:
            binding = await tenant.session.get(SipAccountUpstreamRecord, account_id)
            if binding is None:
                raise SipRouteDeniedError()
            upstream_id, revision = binding.upstream_id, binding.upstream_revision
        return await self.authorize(organization_id, call_id, account_id, upstream_id, revision)

    async def authorize(
        self,
        organization_id: UUID,
        call_id: UUID,
        account_id: UUID,
        upstream_id: UUID,
        upstream_revision: int,
    ) -> SecretStr:
        placement = await self._placement.resolve(organization_id)
        try:
            async with self._database.tenant_transaction(organization_id) as tenant:
                await PlacementAdmission(placement.cell_id).admit(tenant, placement)
                call = await revalidate_call(tenant, call_id, account_id)
                upstream = await tenant.session.get(
                    SipUpstreamRecord, (upstream_id, upstream_revision)
                )
                if upstream is None or upstream.cell_id != placement.cell_id:
                    raise SipRouteDeniedError()
                await self._binding(tenant, account_id, upstream_id, upstream_revision)
                existing = (
                    await tenant.session.execute(
                        select(SipEgressPermitRecord.id).where(
                            SipEgressPermitRecord.call_id == call_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    raise SipConflictError()
                permit_id = uuid7()
                token = self._cipher.encrypt(
                    PermitToken(
                        organization_id=organization_id,
                        permit_id=permit_id,
                        entropy=secrets.token_hex(32),
                    )
                    .model_dump_json()
                    .encode()
                ).decode("ascii")
                now = (await tenant.session.execute(select(func.clock_timestamp()))).scalar_one()
                tenant.session.add(
                    SipEgressPermitRecord(
                        id=permit_id,
                        organization_id=organization_id,
                        call_id=call_id,
                        account_id=account_id,
                        placement_id=placement.placement_id,
                        cell_id=placement.cell_id,
                        placement_generation=placement.assignment_generation,
                        upstream_id=upstream_id,
                        upstream_revision=upstream_revision,
                        asterisk_peer_id=upstream.asterisk_peer_id,
                        destination_digest=fingerprint({"destination": call.to_address}),
                        semantic_digest=fingerprint(
                            {
                                "call": str(call_id),
                                "account": str(account_id),
                                "destination": call.to_address,
                                "upstream": str(upstream_id),
                                "revision": upstream_revision,
                                "generation": placement.assignment_generation,
                            }
                        ),
                        token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
                        state="AUTHORIZED",
                        issued_at=now,
                        expires_at=now + timedelta(seconds=30),
                    )
                )
                await tenant.session.flush()
            return SecretStr(token)
        except IntegrityError:
            raise SipConflictError() from None
        except DBAPIError:
            raise SipRouteUnavailableError() from None

    async def consume(
        self,
        token: SecretStr,
        destination: E164,
        transaction: SipTransaction,
        *,
        edge_id: UUID,
        boot_id: UUID,
        peer_id: UUID,
        observation: PeerObservation,
    ) -> EgressDecision:
        peer = self._peers.authenticate(edge_id, peer_id, observation, direction="OUTBOUND")
        peer_revision = await PeerRegistry(self._database).snapshot(peer)
        plaintext = token.get_secret_value()
        if not 100 <= len(plaintext) <= 2048:
            raise SipRouteDeniedError()
        try:
            binding = PermitToken.model_validate_json(
                self._cipher.decrypt(plaintext.encode("ascii"))
            )
        except InvalidToken, ValidationError, UnicodeError:
            raise SipRouteDeniedError() from None
        placement = await self._placement.resolve(binding.organization_id)
        try:
            async with self._database.tenant_transaction(binding.organization_id) as tenant:
                await PlacementAdmission(placement.cell_id).admit(tenant, placement)
                initial = await tenant.session.get(SipEgressPermitRecord, binding.permit_id)
                if initial is None:
                    raise SipRouteDeniedError()
                call = await revalidate_call(tenant, initial.call_id, initial.account_id)
                upstream = await tenant.session.get(
                    SipUpstreamRecord, (initial.upstream_id, initial.upstream_revision)
                )
                await require_peer_revision(tenant.session, peer_id, peer_revision)
                await self._binding(
                    tenant, initial.account_id, initial.upstream_id, initial.upstream_revision
                )
                permit = (
                    await tenant.session.execute(
                        select(SipEgressPermitRecord)
                        .where(
                            SipEgressPermitRecord.id == binding.permit_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if (
                    upstream is None
                    or peer.cell_id != permit.cell_id
                    or peer.peer_id != permit.asterisk_peer_id
                    or upstream.asterisk_peer_id != peer.peer_id
                    or upstream.cell_id != permit.cell_id
                    or placement.cell_id != permit.cell_id
                    or placement.placement_id != permit.placement_id
                    or placement.assignment_generation != permit.placement_generation
                    or not secrets.compare_digest(
                        permit.token_digest, hashlib.sha256(plaintext.encode("ascii")).hexdigest()
                    )
                    or call.to_address != destination
                    or fingerprint({"destination": destination}) != permit.destination_digest
                ):
                    raise SipRouteDeniedError()
                digest = transaction.digest(peer_id, "OUTBOUND")
                grant = False
                if permit.state == "AUTHORIZED":
                    now = (
                        await tenant.session.execute(select(func.clock_timestamp()))
                    ).scalar_one()
                    if now > permit.expires_at:
                        raise SipRouteDeniedError()
                    permit.state = "CONSUMED"
                    permit.edge_id, permit.boot_id = edge_id, boot_id
                    permit.transaction_digest = digest
                    await tenant.session.flush()
                    grant = True
                elif (permit.edge_id, permit.boot_id, permit.transaction_digest) != (
                    edge_id,
                    boot_id,
                    digest,
                ):
                    raise SipConflictError()
                return EgressDecision(
                    organization_id=permit.organization_id,
                    permit_id=permit.id,
                    initial_relay_granted=grant,
                    host=upstream.host,
                    port=upstream.port,
                    transport=upstream.transport,
                    upstream_id=upstream.id,
                    upstream_revision=upstream.revision,
                )
        except IntegrityError:
            raise SipConflictError() from None
        except DBAPIError:
            raise SipRouteUnavailableError() from None
