"""P11/P18 inbound authority and durable initial-issue fences on PostgreSQL."""

import asyncio
import hmac
import json
import secrets
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.errors import PlacementFencedError
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.api import RouteHandles, create_resolver_app
from nexus_ai.sip_edge.authentication import EdgeAuthenticator
from nexus_ai.sip_edge.contracts import InboundRequest, RegisterTarget, TargetMutation
from nexus_ai.sip_edge.dialogs import DialogResult
from nexus_ai.sip_edge.errors import SipConflictError
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerPolicy, PeerProfile
from nexus_ai.sip_edge.security import EdgeCredential, signing_payload
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
    profile = PeerProfile(
        peer_id=request.peer_id,
        direction="INBOUND",
        edge_ids=(edge,),
        networks=("10.0.0.0/8",),
        transport="UDP",
        isolated_network=True,
        ingress_hosts=("ingress.test",),
    )
    await PeerRegistry(tenant_database).register(actor, profile, expected_revision=0)
    route = await routes.authorize(request, edge_id=edge, boot_id=boot, peer_revision=1)
    credential = EdgeCredential(edge, secrets.token_bytes(32))
    app = create_resolver_app(
        EdgeAuthenticator(tenant_database, (credential,)),
        PeerPolicy(
            (
                PeerProfile(
                    peer_id=request.peer_id,
                    direction="INBOUND",
                    edge_ids=(edge,),
                    networks=("10.0.0.0/8",),
                    transport="UDP",
                    isolated_network=True,
                    ingress_hosts=("ingress.test",),
                ),
            )
        ),
        routes,
        RouteHandles(Fernet.generate_key()),
    )

    async def headers(body: bytes, path: str) -> dict[str, str]:
        async with tenant_database.transaction() as session:
            timestamp = int(
                (await session.execute(select(func.clock_timestamp()))).scalar_one().timestamp()
            )
        nonce = secrets.token_hex(32)
        signature = hmac.digest(
            credential.secret,
            signing_payload(
                edge,
                boot,
                "POST",
                path,
                body,
                timestamp,
                nonce,
            ),
            "sha256",
        ).hex()
        return {
            "x-nxs-edge": str(edge),
            "x-nxs-boot": str(boot),
            "x-nxs-timestamp": str(timestamp),
            "x-nxs-nonce": nonce,
            "x-nxs-signature": signature,
            "content-type": "application/json",
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://resolver"
    ) as client:
        path = "/internal/sip/inbound"
        body = json.dumps(
            {
                "route": request.model_dump(mode="json"),
                "observed_peer": {
                    "source_address": "10.0.0.2",
                    "transport": "UDP",
                },
            }
        ).encode()
        assert (await client.post(path, content=body)).status_code == 403
        signed = await headers(body, path)
        response = await client.post(path, content=body, headers=signed)
        assert response.status_code == 200
        assert response.json()["route"]["route_id"] == str(route.route_id)
        assert (await client.post(path, content=body, headers=signed)).status_code == 403
        forged = json.loads(body)
        forged["route"]["organization_id"] = str(uuid4())
        forged_body = json.dumps(forged).encode()
        assert (
            await client.post(path, content=forged_body, headers=await headers(forged_body, path))
        ).status_code == 403
        issue_body = json.dumps({"handle": response.json()["handle"]}).encode()
        issue_path = "/internal/sip/issue"
        issue_barrier = asyncio.Barrier(2)

        async def issue_http() -> bool:
            signed_issue = await headers(issue_body, issue_path)
            await issue_barrier.wait()
            issued = await client.post(issue_path, content=issue_body, headers=signed_issue)
            assert issued.status_code == 200
            return bool(issued.json()["initial_relay_granted"])

        assert sorted(await asyncio.gather(issue_http(), issue_http())) == [False, True]
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await session.execute(
                text("UPDATE cell_sip_targets SET state='DRAINING' WHERE id=:target"),
                {"target": target.target_id},
            )
    assert route.placement_generation == 1 and route.cell_id == cell
    assert route.model_copy(update={"state": "ISSUED"}) == await routes.authorize(
        request, edge_id=edge, boot_id=boot, peer_revision=1
    )
    with pytest.raises(SipConflictError):
        await routes.authorize(request, edge_id=uuid4(), boot_id=boot, peer_revision=1)
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
        assert sorted(await asyncio.gather(issue(), issue())) == [False, False]
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
        await routes.authorize(request, edge_id=edge, boot_id=boot, peer_revision=1)
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
    result = DialogResult(
        state="ESTABLISHED",
        call_id=request.transaction.call_id,
        from_tag=request.transaction.from_tag,
        to_tag="target-tag",
    )
    arguments = (
        organization.id,
        route.route_id,
        edge,
        boot,
        request.transaction.digest(request.peer_id, "INBOUND"),
    )
    assert await routes.record_result(*arguments, result) == "ESTABLISHED"
    assert await routes.record_result(*arguments, result) == "ESTABLISHED"
    with pytest.raises(SipConflictError):
        await routes.record_result(*arguments, result.model_copy(update={"to_tag": "forged"}))
    assert (
        await routes.record_result(*arguments, result.model_copy(update={"state": "ENDED"}))
        == "ENDED"
    )
    assert (await registry.transition(actor, retirement, uuid4())).state == "RETIRED"
