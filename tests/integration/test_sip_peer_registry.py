"""Durable peer policy revocation defeats stale application configuration."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError
from nexus_ai.sip_edge.peer_registry import PeerRegistry, require_peer_revision
from nexus_ai.sip_edge.peers import PeerProfile
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_peer_revision_race_revocation_history_and_platform_control(
    target_control: Any, tenant_database: Any
) -> None:
    _, actor = target_control
    registry = PeerRegistry(tenant_database)
    profile = PeerProfile(
        peer_id=uuid4(),
        direction="INBOUND",
        edge_ids=(uuid4(),),
        networks=("10.0.0.0/8",),
        transport="UDP",
        isolated_network=True,
        ingress_hosts=("ingress.test",),
    )
    with pytest.raises(PermissionDeniedError):
        await registry.register(uuid4(), profile, expected_revision=0)
    assert await registry.register(actor, profile, expected_revision=0) == 1
    assert await registry.register(actor, profile, expected_revision=0) == 1
    assert await registry.snapshot(profile) == 1
    barrier = asyncio.Barrier(2)

    async def revise(host: str) -> Any:
        await barrier.wait()
        try:
            return await registry.register(
                actor, profile.model_copy(update={"ingress_hosts": (host,)}), expected_revision=1
            )
        except SipConflictError:
            return "CONFLICT"

    results = await asyncio.gather(revise("first.test"), revise("second.test"))
    assert results.count(2) == 1 and results.count("CONFLICT") == 1
    with pytest.raises(SipRouteDeniedError):
        await registry.snapshot(profile)
    async with tenant_database.transaction() as session:
        with pytest.raises(SipRouteDeniedError):
            await require_peer_revision(session, profile.peer_id, 1)
    assert await registry.register(actor, profile, expected_revision=2, active=False) == 3
    with pytest.raises(SipRouteDeniedError):
        await registry.snapshot(profile)
    async with tenant_database.transaction() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM sip_peer_history WHERE peer_id=:peer"),
                {"peer": profile.peer_id},
            )
        ).scalar_one() == 3
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE sip_peer_profiles SET revision=revision+1, active=true WHERE id=:peer"
                ),
                {"peer": profile.peer_id},
            )
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await session.execute(
                text("DELETE FROM sip_peer_history WHERE peer_id=:peer"), {"peer": profile.peer_id}
            )
