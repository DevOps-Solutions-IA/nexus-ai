"""DB-time expiry denies consumption without resetting the logical-call fence."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerObservation, PeerPolicy, PeerProfile
from nexus_ai.sip_edge.upstreams import RegisterUpstream, UpstreamRegistry
from nexus_ai.telephony.entities import CreateCallRequest
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_route_matrix import routing as routing
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_unconsumed_permit_expires_on_database_time_and_cannot_be_reissued(
    routing: Any, telephony_stack: Any
) -> None:
    edge, peer, upstream = uuid4(), uuid4(), uuid4()
    profile = PeerProfile(
        peer_id=peer,
        direction="OUTBOUND",
        edge_ids=(edge,),
        networks=("10.0.0.0/8",),
        transport="UDP",
        isolated_network=True,
        cell_id=routing.cell,
    )
    await PeerRegistry(routing.database).register(routing.actor, profile, expected_revision=0)
    registry = UpstreamRegistry(routing.database)
    await registry.register(
        routing.actor,
        RegisterUpstream(
            id=upstream,
            revision=1,
            host="10.9.8.7",
            port=5060,
            transport="UDP",
            cell_id=routing.cell,
            asterisk_peer_id=peer,
        ),
    )
    await registry.bind_account(
        routing.actor, routing.organization.id, routing.account.id, upstream, 1, expected_revision=0
    )
    call = await telephony_stack.service.create_call(
        routing.organization.id,
        None,
        CreateCallRequest(
            provider_account_id=routing.account.id,
            from_number_id=routing.number.id,
            destination="+12025550789",
            idempotency_key=uuid4().hex,
        ),
    )
    permits = EgressPermits(routing.database, Fernet.generate_key(), PeerPolicy((profile,)))
    token = await permits.authorize(
        routing.organization.id, call.id, routing.account.id, upstream, 1
    )
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        expires, issued, now = (
            await tenant.session.execute(
                text(
                    "SELECT expires_at, issued_at, clock_timestamp() FROM sip_egress_permits "
                    "WHERE call_id=:call"
                ),
                {"call": call.id},
            )
        ).one()
    assert (expires - issued).total_seconds() == 30
    remaining = (expires - now).total_seconds()
    assert 0 < remaining <= 30
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.Event().wait(), timeout=remaining + 0.01)
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        assert (
            await tenant.session.execute(
                text("SELECT clock_timestamp() >= :expires"), {"expires": expires}
            )
        ).scalar_one()
    with pytest.raises(SipRouteDeniedError):
        await permits.consume(
            token,
            "+12025550789",
            routing.request.transaction,
            edge_id=edge,
            boot_id=uuid4(),
            peer_id=peer,
            observation=PeerObservation(source_address="10.0.0.2", transport="UDP"),
        )
    with pytest.raises(SipConflictError):
        await permits.authorize(routing.organization.id, call.id, routing.account.id, upstream, 1)
    with pytest.raises(DBAPIError):
        async with routing.database.tenant_transaction(routing.organization.id) as tenant:
            await tenant.session.execute(
                text(
                    "UPDATE sip_egress_permits SET state='CONSUMED', edge_id=:edge, "
                    "boot_id=:boot, transaction_digest=:digest WHERE call_id=:call"
                ),
                {"edge": edge, "boot": uuid4(), "digest": "a" * 64, "call": call.id},
            )
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_egress_routes"))
        ).scalar_one() == 0
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_egress_permits"))
        ).scalar_one() == 1
