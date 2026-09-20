"""Outbound permits are durable, tenant-confined and single-consumption."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text

from nexus_ai.cells.contracts import PlacementMutation, RegisterCellRequest
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.contracts import SipTransaction
from nexus_ai.sip_edge.dialogs import DialogResult
from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerObservation, PeerPolicy, PeerProfile
from nexus_ai.sip_edge.upstreams import RegisterUpstream, UpstreamRegistry
from nexus_ai.telephony.entities import CreateCallRequest
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("lose_committed_response", [False, True])
@pytest.mark.parametrize("independent_transactions", [False, True])
async def test_egress_single_consumption_wrong_peer_destination_and_response_loss(
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    lose_committed_response: bool,
    independent_transactions: bool,
) -> None:
    cell, actor = target_control
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550888")
    await CellPlacementService(tenant_database, event_platform.publisher).mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    call = await telephony_stack.service.create_call(
        organization.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id,
            from_number_id=number.id,
            destination="+12025550889",
            idempotency_key=uuid4().hex,
        ),
    )
    edge, peer, upstream = uuid4(), uuid4(), uuid4()
    await PeerRegistry(tenant_database).register(
        actor,
        PeerProfile(
            peer_id=peer,
            direction="OUTBOUND",
            edge_ids=(edge,),
            networks=("10.0.0.0/8",),
            transport="UDP",
            isolated_network=True,
            cell_id=cell,
        ),
        expected_revision=0,
    )
    profiles = PeerPolicy(
        (
            PeerProfile(
                peer_id=peer,
                direction="OUTBOUND",
                edge_ids=(edge,),
                networks=("10.0.0.0/8",),
                transport="UDP",
                isolated_network=True,
                cell_id=cell,
            ),
        )
    )
    other_cell = await CellPlacementService(tenant_database, event_platform.publisher).register(
        actor,
        RegisterCellRequest(
            cell_key="egress-" + uuid4().hex, idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    wrong_peer = PeerProfile(
        peer_id=uuid4(),
        direction="OUTBOUND",
        edge_ids=(edge,),
        networks=("10.0.0.0/8",),
        transport="UDP",
        isolated_network=True,
        cell_id=other_cell,
    )
    await PeerRegistry(tenant_database).register(actor, wrong_peer, expected_revision=0)
    await UpstreamRegistry(tenant_database).register(
        actor,
        RegisterUpstream(
            id=upstream,
            revision=1,
            host="10.5.0.1",
            port=5060,
            transport="UDP",
            cell_id=cell,
            asterisk_peer_id=peer,
        ),
    )
    await UpstreamRegistry(tenant_database).bind_account(
        actor, organization.id, account.id, upstream, 1, expected_revision=0
    )
    permits = EgressPermits(tenant_database, Fernet.generate_key(), profiles)
    token = await permits.authorize(organization.id, call.id, account.id, upstream, 1)
    assert token.get_secret_value() not in repr(token)
    with pytest.raises(SipConflictError):
        await permits.authorize(organization.id, call.id, account.id, upstream, 1)
    transaction = SipTransaction(
        call_id=uuid4().hex,
        from_tag="tag",
        cseq=1,
        via_branch="z9hG4bK" + uuid4().hex,
        via_sent_by="asterisk.test:5060",
    )
    arguments = dict(
        edge_id=edge,
        boot_id=uuid4(),
        peer_id=peer,
        observation=PeerObservation(source_address="10.0.0.2", transport="UDP"),
    )
    with pytest.raises(SipRouteDeniedError):
        await permits.consume(token, "+12025550000", transaction, **arguments)
    with pytest.raises(SipRouteDeniedError):
        await permits.consume(SecretStr("a" * 256), "+12025550889", transaction, **arguments)
    with pytest.raises(SipRouteDeniedError):
        await permits.consume(
            token, "+12025550889", transaction, **(arguments | {"peer_id": uuid4()})
        )
    permits._peers = PeerPolicy((*profiles._profiles.values(), wrong_peer))
    with pytest.raises(SipRouteDeniedError):
        await permits.consume(
            token, "+12025550889", transaction, **(arguments | {"peer_id": wrong_peer.peer_id})
        )
    barrier = asyncio.Barrier(2)
    committed: list[Any] = []

    async def consume(attempt: SipTransaction) -> Any:
        await barrier.wait()
        result = await permits.consume(token, "+12025550889", attempt, **arguments)
        if result.initial_relay_granted:
            committed.append((attempt, result.permit_id))
        if lose_committed_response and result.initial_relay_granted:
            raise ConnectionResetError("injected response loss after committed consumption")
        return result

    async with asyncio.timeout(10):
        alternative = (
            transaction.model_copy(update={"call_id": uuid4().hex})
            if independent_transactions
            else transaction
        )
        results = await asyncio.gather(
            consume(transaction), consume(alternative), return_exceptions=True
        )
    assert len(committed) == 1
    transaction = committed[0][0]
    if independent_transactions:
        assert sum(isinstance(result, SipConflictError) for result in results) == 1
    if lose_committed_response:
        assert sum(isinstance(result, ConnectionResetError) for result in results) == 1
        delivered = [result for result in results if not isinstance(result, BaseException)]
        assert len(delivered) == (0 if independent_transactions else 1)
        assert all(result.initial_relay_granted is False for result in delivered)
    elif independent_transactions:
        assert (
            sum(
                not isinstance(result, BaseException) and result.initial_relay_granted
                for result in results
            )
            == 1
        )
    else:
        assert not any(isinstance(result, BaseException) for result in results)
        assert sorted(result.initial_relay_granted for result in results) == [False, True]
        assert results[0].permit_id == results[1].permit_id
    replay = await permits.consume(token, "+12025550889", transaction, **arguments)
    assert not replay.initial_relay_granted
    assert replay.upstream_id == upstream
    with pytest.raises(SipConflictError):
        await permits.consume(
            token,
            "+12025550889",
            transaction.model_copy(update={"call_id": uuid4().hex}),
            **arguments,
        )
    with pytest.raises(SipConflictError):
        await permits.authorize(organization.id, call.id, account.id, upstream, 1)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state, token_digest FROM sip_egress_permits WHERE call_id=:call"),
                {"call": call.id},
            )
        ).one()
        assert state.state == "CONSUMED"
        assert state.token_digest != token.get_secret_value()
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_egress_routes"))
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(
                text("SELECT state FROM sip_egress_history ORDER BY occurred_at")
            )
        ).scalars().all() == ["AUTHORIZED", "CONSUMED"]
    established = DialogResult(
        state="ESTABLISHED",
        call_id=transaction.call_id,
        from_tag=transaction.from_tag,
        to_tag="carrier-dialog",
    )
    digest = transaction.digest(peer, "OUTBOUND")

    async def record(result: DialogResult, boot: Any = arguments["boot_id"]) -> str:
        return await permits.record_result(
            organization.id, replay.permit_id, edge, boot, digest, result
        )

    with pytest.raises(SipRouteDeniedError):
        await record(established, uuid4())
    assert await record(established) == "ESTABLISHED"
    assert await record(established) == "ESTABLISHED"
    with pytest.raises(SipConflictError):
        await record(established.model_copy(update={"to_tag": "forged"}))
    await CellPlacementService(tenant_database, event_platform.publisher).mutate(
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
    ended = established.model_copy(update={"state": "ENDED"})
    assert await record(ended) == "ENDED"
    assert await record(ended) == "ENDED"
    with pytest.raises(SipConflictError):
        await record(established)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(
                text("SELECT state FROM sip_egress_history ORDER BY occurred_at")
            )
        ).scalars().all() == ["AUTHORIZED", "CONSUMED", "ENDED"]
