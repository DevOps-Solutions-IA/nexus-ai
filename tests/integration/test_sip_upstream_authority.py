"""Upstream rotation cannot redirect a previously issued call permit."""

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


async def test_upstream_rotation_fences_old_permit_and_preserves_append_only_history(
    routing: Any, telephony_stack: Any, make_organization: Any
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
    for revision in (1, 2, 3):
        await registry.register(
            routing.actor,
            RegisterUpstream(
                id=upstream,
                revision=revision,
                host=f"10.9.8.{revision}",
                port=5060,
                transport="UDP",
                cell_id=routing.cell,
                asterisk_peer_id=peer,
            ),
        )
    await registry.bind_account(
        routing.actor,
        routing.organization.id,
        routing.account.id,
        upstream,
        1,
        expected_revision=0,
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
    token = await permits.issue_for_call(routing.organization.id, call.id, routing.account.id)
    barrier = asyncio.Barrier(2)

    async def rotate(revision: int) -> int:
        await barrier.wait()
        return await registry.bind_account(
            routing.actor,
            routing.organization.id,
            routing.account.id,
            upstream,
            revision,
            expected_revision=1,
        )

    results = await asyncio.gather(rotate(2), rotate(3), return_exceptions=True)
    assert sum(result == 2 for result in results) == 1
    assert sum(isinstance(result, SipConflictError) for result in results) == 1
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
        await permits.issue_for_call(routing.organization.id, call.id, routing.account.id)
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        history = (
            await tenant.session.execute(
                text(
                    "SELECT revision, upstream_revision, actor_user_id "
                    "FROM sip_account_upstream_history ORDER BY revision"
                )
            )
        ).all()
        assert [entry.revision for entry in history] == [1, 2]
        assert history[0].upstream_revision == 1
        assert history[1].upstream_revision in (2, 3)
        assert all(entry.actor_user_id == routing.actor for entry in history)
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_egress_routes"))
        ).scalar_one() == 0
        for statement in (
            "DELETE FROM sip_account_upstreams",
            "DELETE FROM sip_account_upstream_history",
            "UPDATE sip_account_upstream_history SET revision=99",
        ):
            try:
                async with tenant.session.begin_nested():
                    result = await tenant.session.execute(text(statement))
                    assert result.rowcount == 0
            except DBAPIError as error:
                assert error.orig.sqlstate == "42501"
    foreign = await make_organization()
    async with routing.database.tenant_transaction(foreign.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_account_upstream_history"))
        ).scalar_one() == 0
    with pytest.raises(DBAPIError):
        async with routing.database.tenant_transaction(routing.organization.id) as tenant:
            await tenant.session.execute(
                text("UPDATE sip_account_upstreams SET revision=revision+1")
            )
