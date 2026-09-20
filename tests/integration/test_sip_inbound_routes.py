"""P11/P18 inbound authority and durable initial-issue fences on PostgreSQL."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.errors import PlacementFencedError
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.contracts import InboundRequest, RegisterTarget, TargetMutation
from nexus_ai.sip_edge.errors import SipConflictError
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.targets import TargetNetworkPolicy, TargetRegistry
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_inbound_authority_issue_race_and_retirement_fence(
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
) -> None:
    cell, actor = target_control
    organization = await make_organization()
    _, number = await provision(telephony_stack, organization, "+12025550777")
    placement_service = CellPlacementService(tenant_database, event_platform.publisher)
    await placement_service.mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell,
            operation="ASSIGN",
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    registry = TargetRegistry(
        tenant_database, TargetNetworkPolicy(("10.0.0.0/8",), frozenset({5060}))
    )
    target = await registry.register(
        actor,
        RegisterTarget(
            cell_id=cell,
            host="10.1.2.3",
            port=5060,
            transport="UDP",
            expected_revision=0,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    await registry.transition(
        actor,
        TargetMutation(
            cell_id=cell,
            target_id=target.target_id,
            state="ACTIVE",
            expected_revision=1,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    routes = InboundRoutes(tenant_database, DidLocator(discovery_database))
    edge, boot = uuid4(), uuid4()
    request = InboundRequest(
        peer_id=uuid4(),
        called_number=number.e164,
        ingress_host="ingress.test",
        transaction=dict(
            call_id=uuid4().hex,
            from_tag="tag",
            cseq=1,
            via_branch="z9hG4bK" + uuid4().hex,
            via_sent_by="peer.test:5060",
        ),
    )
    route = await routes.authorize(request, edge_id=edge, boot_id=boot)
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await session.execute(
                text("UPDATE cell_sip_targets SET state='DRAINING' WHERE id=:target"),
                {"target": target.target_id},
            )
    assert route.placement_generation == 1 and route.cell_id == cell
    assert route == await routes.authorize(request, edge_id=edge, boot_id=boot)
    with pytest.raises(SipConflictError):
        await routes.authorize(request, edge_id=uuid4(), boot_id=boot)
    barrier = asyncio.Barrier(2)

    async def issue() -> bool:
        await barrier.wait()
        return await routes.issue(
            organization.id,
            route.route_id,
            edge_id=edge,
            boot_id=boot,
            transaction_digest=request.transaction.digest(request.peer_id, "INBOUND"),
        )

    async with asyncio.timeout(10):
        assert sorted(await asyncio.gather(issue(), issue())) == [False, True]
    await placement_service.mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell,
            operation="SUSPEND",
            expected_generation=1,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    with pytest.raises(PlacementFencedError):
        await routes.authorize(request, edge_id=edge, boot_id=boot)
    await registry.transition(
        actor,
        TargetMutation(
            cell_id=cell,
            target_id=target.target_id,
            state="DRAINING",
            expected_revision=2,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    retirement = TargetMutation(
        cell_id=cell,
        target_id=target.target_id,
        state="RETIRED",
        expected_revision=3,
        idempotency_key=uuid4().hex,
        reason_code="TEST",
    )
    with pytest.raises(SipConflictError):
        await registry.transition(actor, retirement, uuid4())
    async with tenant_database.transaction() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM sip_route_authorizations"))
        ).scalar_one() == 0
        assert (
            await session.execute(text("DELETE FROM sip_target_route_references"))
        ).rowcount == 0
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await tenant.session.execute(
            text("UPDATE sip_route_authorizations SET state='ESTABLISHED' WHERE id=:route"),
            {"route": route.route_id},
        )
        await tenant.session.execute(
            text("UPDATE sip_route_authorizations SET state='ENDED' WHERE id=:route"),
            {"route": route.route_id},
        )
    assert (await registry.transition(actor, retirement, uuid4())).state == "RETIRED"
