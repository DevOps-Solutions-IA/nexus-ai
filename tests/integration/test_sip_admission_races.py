"""Independent PostgreSQL sessions linearize new SIP authority with P18 suspension."""

import asyncio
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

import nexus_ai.sip_edge.inbound as inbound_module
from nexus_ai.cells.admission import PlacementAdmission
from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.errors import PlacementFencedError
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.contracts import InboundRequest, RegisterTarget, TargetMutation
from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerProfile
from nexus_ai.sip_edge.targets import TargetNetworkPolicy, TargetRegistry
from nexus_ai.telephony.entities import AccountStatus
from tests.conftest import RUNTIME_DSN
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("authorization_first", [False, True])
@pytest.mark.parametrize("mutation", ["placement", "number", "account"])
async def test_suspension_linearizes_with_new_route_admission(
    authorization_first: bool,
    mutation: str,
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cell, actor = target_control
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550670")
    placement = CellPlacementService(tenant_database, event_platform.publisher)
    await placement.mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    targets = TargetRegistry(
        tenant_database, TargetNetworkPolicy(("10.0.0.0/8",), frozenset({5060}))
    )
    target = await targets.register(
        actor,
        RegisterTarget(
            cell_id=cell,
            host="10.1.1.10",
            port=5060,
            transport="UDP",
            expected_revision=0,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    await targets.transition(
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
    edge, boot, peer = uuid4(), uuid4(), uuid4()
    await PeerRegistry(tenant_database).register(
        actor,
        PeerProfile(
            peer_id=peer,
            direction="INBOUND",
            edge_ids=(edge,),
            networks=("10.0.0.0/8",),
            transport="UDP",
            isolated_network=True,
            ingress_hosts=("ingress.test",),
        ),
        expected_revision=0,
    )
    request = InboundRequest(
        peer_id=peer,
        called_number=number.e164,
        ingress_host="ingress.test",
        transaction={
            "call_id": uuid4().hex,
            "from_tag": "race",
            "cseq": 1,
            "via_branch": "z9hG4bK" + uuid4().hex,
            "via_sent_by": "carrier.test:5060",
        },
    )
    routes = InboundRoutes(tenant_database, DidLocator(discovery_database))
    reached, release = asyncio.Event(), asyncio.Event()
    backend: list[int] = []
    original_source = inbound_module.revalidate_source
    original_admit = PlacementAdmission.admit

    async def held_source(tenant: Any, candidate: Any) -> None:
        await original_source(tenant, candidate)
        backend.append((await tenant.session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        reached.set()
        await release.wait()

    async def held_admission(admission: Any, tenant: Any, expected: Any) -> None:
        reached.set()
        await release.wait()
        await original_admit(admission, tenant, expected)

    if authorization_first:
        monkeypatch.setattr(inbound_module, "revalidate_source", held_source)
    else:
        monkeypatch.setattr(PlacementAdmission, "admit", held_admission)

    async def suspend() -> None:
        if mutation == "number":
            await telephony_stack.service.set_number_verified(
                organization.id, number.id, verified=False
            )
            return
        if mutation == "account":
            await telephony_stack.service.set_account_status(
                organization.id, account.id, AccountStatus.DISABLED
            )
            return
        await placement.mutate(
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

    async with asyncio.timeout(10):
        authorization = asyncio.create_task(
            routes.authorize(request, edge_id=edge, boot_id=boot, peer_revision=1)
        )
        await reached.wait()
        suspension = asyncio.create_task(suspend())
        try:
            if authorization_first:
                async with tenant_database.transaction() as session:
                    for _ in range(1000):
                        await session.execute(text("SELECT pg_stat_clear_snapshot()"))
                        waiting = (
                            await session.execute(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                    "WHERE :blocker = ANY(pg_blocking_pids(pid)))"
                                ),
                                {"blocker": backend[0]},
                            )
                        ).scalar_one()
                        if waiting:
                            break
                    assert waiting, (
                        "suspension never waited on the actual PostgreSQL authority lock"
                    )
                release.set()
                route = await authorization
                await suspension
                assert await routes.issue(
                    organization.id,
                    route.route_id,
                    edge_id=edge,
                    boot_id=boot,
                    transaction_digest=request.transaction.digest(peer, "INBOUND"),
                )
            else:
                await suspension
                release.set()
                with pytest.raises(
                    PlacementFencedError if mutation == "placement" else SipRouteDeniedError
                ):
                    await authorization
                async with tenant_database.tenant_transaction(organization.id) as tenant:
                    assert (
                        await tenant.session.execute(
                            text("SELECT count(*) FROM sip_route_authorizations")
                        )
                    ).scalar_one() == 0
        finally:
            release.set()
            await asyncio.gather(authorization, suspension, return_exceptions=True)


async def test_lock_observer_refreshes_new_postgresql_sessions(tenant_database: Any) -> None:
    async with tenant_database.transaction() as observer:
        await observer.execute(text("SELECT pid FROM pg_stat_activity"))
        contender = await asyncpg.connect(
            make_url(RUNTIME_DSN).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        try:
            backend = await contender.fetchval("SELECT pg_backend_pid()")
            query = text("SELECT count(*) FROM pg_stat_activity WHERE pid=:backend")
            assert (await observer.execute(query, {"backend": backend})).scalar_one() == 0
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            assert (await observer.execute(query, {"backend": backend})).scalar_one() == 1
        finally:
            await contender.close()
