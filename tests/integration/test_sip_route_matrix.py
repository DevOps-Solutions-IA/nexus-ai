"""Authority, failure and independent-connection tests for new SIP routes."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

import nexus_ai.sip_edge.inbound as inbound_module
from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.errors import PlacementFencedError
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.domain.sip_edge.models import SipRouteAuthorizationRecord
from nexus_ai.infrastructure.cache import Cache
from nexus_ai.sip_edge.contracts import InboundRequest, RegisterTarget, TargetMutation
from nexus_ai.sip_edge.errors import SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerProfile
from nexus_ai.sip_edge.targets import TargetNetworkPolicy, TargetRegistry
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def routing(
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
) -> Any:
    cell, actor = target_control
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550680")
    placement = CellPlacementService(tenant_database, event_platform.publisher)
    await placement.mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
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
    edges, peer, boot = (uuid4(), uuid4()), uuid4(), uuid4()
    await PeerRegistry(tenant_database).register(
        actor,
        PeerProfile(
            peer_id=peer,
            direction="INBOUND",
            edge_ids=edges,
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
        transaction=dict(
            call_id=uuid4().hex,
            from_tag="matrix",
            cseq=1,
            via_branch="z9hG4bK" + uuid4().hex,
            via_sent_by="carrier.test:5060",
        ),
    )
    return SimpleNamespace(
        cell=cell,
        actor=actor,
        organization=organization,
        account=account,
        number=number,
        edges=edges,
        peer=peer,
        boot=boot,
        request=request,
        registry=registry,
        target=target,
        placement=placement,
        routes=InboundRoutes(tenant_database, DidLocator(discovery_database)),
        discovery=discovery_database,
        database=tenant_database,
    )


async def authorize(context: Any, index: int = 0, *, request: Any = None) -> Any:
    return await context.routes.authorize(
        request or context.request,
        edge_id=context.edges[index],
        boot_id=context.boot,
        peer_revision=1,
    )


async def route_count(context: Any) -> int:
    async with context.database.tenant_transaction(context.organization.id) as tenant:
        return int(
            (
                await tenant.session.execute(text("SELECT count(*) FROM sip_route_authorizations"))
            ).scalar_one()
        )


async def test_route_history_and_dialog_raw_sql_cannot_cross_tenant(
    routing: Any,
    make_organization: Any,
) -> None:
    route = await authorize(routing)
    foreign = await make_organization()
    async with routing.database.tenant_transaction(foreign.id) as tenant:
        role = (
            await tenant.session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert tuple(role) == (False, False)
        for statement in (
            "SELECT count(*) FROM sip_route_authorizations",
            "SELECT count(*) FROM sip_route_history",
            "SELECT count(*) FROM sip_dialog_bindings",
        ):
            assert (await tenant.session.execute(text(statement))).scalar_one() == 0
        changed = await tenant.session.execute(
            text("UPDATE sip_route_authorizations SET state='ISSUED' WHERE id=:route"),
            {"route": route.route_id},
        )
        assert changed.rowcount == 0
    for statement, parameters in (
        (
            "INSERT INTO sip_dialog_bindings (route_id, organization_id, dialog_digest) "
            "VALUES (:route, :organization, :digest)",
            {"route": route.route_id, "organization": foreign.id, "digest": "a" * 64},
        ),
        (
            "INSERT INTO sip_route_history "
            "(id, organization_id, route_id, state, edge_id, boot_id) "
            "VALUES (:id, :organization, :route, 'ISSUED', :edge, :boot)",
            {
                "id": uuid4(),
                "organization": foreign.id,
                "route": route.route_id,
                "edge": routing.edges[0],
                "boot": routing.boot,
            },
        ),
    ):
        with pytest.raises(DBAPIError) as denied:
            async with routing.database.tenant_transaction(foreign.id) as tenant:
                await tenant.session.execute(text(statement), parameters)
        assert denied.value.orig.sqlstate in {"23503", "23514"}
    with pytest.raises(DBAPIError):
        async with routing.database.tenant_transaction(foreign.id) as tenant:
            await tenant.session.execute(text("SET LOCAL row_security=off"))
            await tenant.session.execute(text("SELECT * FROM sip_route_authorizations"))
    with pytest.raises(SipRouteDeniedError):
        await routing.routes.issue(
            foreign.id,
            route.route_id,
            edge_id=routing.edges[0],
            boot_id=routing.boot,
            transaction_digest=routing.request.transaction.digest(routing.peer, "INBOUND"),
        )
    assert await route_count(routing) == 1


async def test_cache_bus_absence_and_reordered_hints_cannot_grant_route_authority(
    routing: Any,
    nats_messaging: Any,
    integration_env: Any,
) -> None:
    cache = Cache(integration_env().cache)
    await cache.connect()
    key = "p19-test-hint:" + uuid4().hex
    try:
        for generation in (99, 1, 99):
            hint = json.dumps(
                {
                    "organization_id": str(routing.organization.id),
                    "cell_id": str(uuid4()),
                    "placement_generation": generation,
                    "state": "ACTIVE",
                }
            )
            await cache.client.set(key, hint, ex=30)
            await nats_messaging.client.publish("p19.test.untrusted_hint", hint.encode())
        await nats_messaging.client.flush()
        route = await authorize(routing)
        assert route.cell_id == routing.cell and route.placement_generation == 1
        await routing.placement.mutate(
            routing.organization.id,
            routing.actor,
            PlacementMutation(
                cell_id=routing.cell,
                operation="SUSPEND",
                expected_generation=1,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
        await cache.client.delete(key)
        await cache.disconnect()
        await nats_messaging.disconnect()
        assert not cache.is_connected and not nats_messaging.is_connected
        with pytest.raises(PlacementFencedError):
            await authorize(
                routing,
                request=routing.request.model_copy(
                    update={
                        "transaction": routing.request.transaction.model_copy(
                            update={"call_id": uuid4().hex}
                        )
                    }
                ),
            )
        assert await route_count(routing) == 1
    finally:
        await cache.disconnect()
        await nats_messaging.connect()


async def test_unissued_inbound_deadline_cannot_be_extended_or_relayed(routing: Any) -> None:
    route = await authorize(routing)
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        remaining = (
            await tenant.session.execute(
                text(
                    "SELECT extract(epoch FROM issue_deadline-clock_timestamp()) "
                    "FROM sip_route_authorizations WHERE id=:route"
                ),
                {"route": route.route_id},
            )
        ).scalar_one()
    assert 0 < remaining <= 5
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.Event().wait(), timeout=float(remaining) + 0.01)
    assert not await routing.routes.issue(
        routing.organization.id,
        route.route_id,
        edge_id=routing.edges[0],
        boot_id=routing.boot,
        transaction_digest=routing.request.transaction.digest(routing.peer, "INBOUND"),
    )
    replay = await authorize(routing)
    assert replay.route_id == route.route_id and replay.issue_deadline == route.issue_deadline
    assert replay.state == "EXPIRED"
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM sip_target_route_references WHERE route_id=:route"),
                {"route": route.route_id},
            )
        ).scalar_one() == 0
        assert (
            await tenant.session.execute(
                text("SELECT state FROM sip_route_history ORDER BY occurred_at")
            )
        ).scalars().all() == ["AUTHORIZED", "EXPIRED"]


async def test_concurrent_independent_edges_same_authoritative_tuple(
    routing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = asyncio.Barrier(2)
    connections: list[int] = []
    revalidate = inbound_module.revalidate_source

    async def synchronized_source(tenant: Any, candidate: Any) -> None:
        await revalidate(tenant, candidate)
        connections.append(
            (await tenant.session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        await barrier.wait()

    monkeypatch.setattr(inbound_module, "revalidate_source", synchronized_source)

    async def resolve(index: int) -> Any:
        request = routing.request.model_copy(
            update={
                "transaction": routing.request.transaction.model_copy(
                    update={"call_id": uuid4().hex}
                )
            }
        )
        await barrier.wait()
        return await authorize(routing, index, request=request)

    async with asyncio.timeout(10):
        first, second = await asyncio.gather(resolve(0), resolve(1))
    for field in (
        "organization_id",
        "cell_id",
        "placement_generation",
        "target_id",
        "target_revision",
        "target_host",
        "target_port",
        "target_transport",
    ):
        assert getattr(first, field) == getattr(second, field)
    assert first.route_id != second.route_id
    assert first.edge_id != second.edge_id
    assert len(set(connections)) == 2
    assert await route_count(routing) == 2


async def test_target_rotation_and_resolution_never_mix_tuple(routing: Any) -> None:
    replacement = await routing.registry.register(
        routing.actor,
        RegisterTarget(
            cell_id=routing.cell,
            host="10.9.8.7",
            port=5060,
            transport="UDP",
            expected_revision=2,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    barrier = asyncio.Barrier(2)

    async def resolve() -> Any:
        await barrier.wait()
        return await authorize(routing)

    async def rotate() -> Any:
        await barrier.wait()
        return await routing.registry.transition(
            routing.actor,
            TargetMutation(
                cell_id=routing.cell,
                target_id=replacement.target_id,
                state="ACTIVE",
                expected_revision=3,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )

    async with asyncio.timeout(10):
        route, _ = await asyncio.gather(resolve(), rotate())
    assert (route.target_id, route.target_host, route.target_revision) in {
        (routing.target.target_id, "10.1.2.3", 1),
        (replacement.target_id, "10.9.8.7", 3),
    }


@pytest.mark.parametrize("failure", ["unknown_did", "missing_target", "disconnected_discovery"])
async def test_failures_create_zero_route_authority(routing: Any, failure: str) -> None:
    request = routing.request
    error = SipRouteDeniedError
    if failure == "unknown_did":
        request = request.model_copy(update={"called_number": "+12025550000"})
    elif failure == "missing_target":
        await routing.registry.transition(
            routing.actor,
            TargetMutation(
                cell_id=routing.cell,
                target_id=routing.target.target_id,
                state="DRAINING",
                expected_revision=2,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
    else:
        await routing.discovery.disconnect()
        error = SipRouteUnavailableError
    try:
        with pytest.raises(error):
            await authorize(routing, request=request)
        assert await route_count(routing) == 0
    finally:
        if failure == "disconnected_discovery":
            await routing.discovery.connect()
    if failure == "disconnected_discovery":
        assert (await authorize(routing)).cell_id == routing.cell


async def test_reactivation_preserves_cell_and_advances_new_dialog_generation(routing: Any) -> None:
    original = await authorize(routing)
    for generation, operation in ((1, "SUSPEND"), (2, "RESUME")):
        await routing.placement.mutate(
            routing.organization.id,
            routing.actor,
            PlacementMutation(
                cell_id=routing.cell,
                operation=operation,
                expected_generation=generation,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
    request = routing.request.model_copy(
        update={
            "transaction": routing.request.transaction.model_copy(update={"call_id": uuid4().hex})
        }
    )
    current = await authorize(routing, request=request)
    assert current.cell_id == original.cell_id
    assert original.placement_generation == 1 and current.placement_generation == 3


async def test_route_reference_and_history_rollback_atomically(
    routing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    flush = AsyncSession.flush

    async def failing_flush(session: Any, objects: Any = None) -> None:
        inserted = any(isinstance(item, SipRouteAuthorizationRecord) for item in session.new)
        await flush(session, objects)
        if inserted:
            raise RuntimeError("injected after route/reference/history SQL, before commit")

    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, "flush", failing_flush)
        with pytest.raises(RuntimeError, match="before commit"):
            await authorize(routing)
    assert await route_count(routing) == 0
    async with routing.database.tenant_transaction(routing.organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_route_history"))
        ).scalar_one() == 0
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM sip_target_route_references WHERE target_id=:target"),
                {"target": routing.target.target_id},
            )
        ).scalar_one() == 0
    assert (await authorize(routing)).target_id == routing.target.target_id
