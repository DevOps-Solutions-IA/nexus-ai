"""Pre-ARI authority denial is terminal, unlike an ambiguous provider timeout."""

from typing import Any
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text

from nexus_ai.cells.admission import PlacementResolver
from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.errors import PlacementFencedError
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.errors import SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerPolicy, PeerProfile
from nexus_ai.sip_edge.upstreams import RegisterUpstream, UpstreamRegistry
from nexus_ai.telephony.entities import CallState, CreateCallRequest, TelephonyProvider
from nexus_ai.telephony.errors import TelephonyIdempotencyConflictError
from nexus_ai.telephony.service import TelephonyAdmissionUnavailableError, TelephonyService
from tests.integration.test_sip_target_constraints import target_control as target_control
from tests.integration.test_telephony_service import _account, _number

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize(
    "failure",
    [
        "missing_upstream",
        "suspended",
        "stale_generation",
        "unavailable",
        "timeout",
        "database_disconnect",
    ],
)
async def test_permit_denial_is_terminal_and_replay_never_originates(
    telephony_stack: Any, make_organization: Any, failure: str
) -> None:
    stack = telephony_stack
    organization = await make_organization()
    account = await _account(stack, organization.id, provider=TelephonyProvider.ASTERISK)
    number = await _number(stack, organization.id, account)
    attempted: list[Any] = []
    errors = {
        "missing_upstream": SipRouteDeniedError,
        "suspended": PlacementFencedError,
        "stale_generation": PlacementFencedError,
        "unavailable": SipRouteUnavailableError,
        "timeout": TimeoutError,
    }

    class RejectingAuthority:
        async def issue_for_call(
            self, organization_id: Any, call_id: Any, account_id: Any
        ) -> SecretStr:
            async with stack.database.tenant_transaction(organization_id) as tenant:
                row = (
                    await tenant.session.execute(
                        text("SELECT id, state FROM telephony_calls WHERE id=:id"), {"id": call_id}
                    )
                ).one()
                assert row.state == "CREATED"
                attempted.append(row.id)
            if failure == "database_disconnect":
                async with stack.database.tenant_transaction(organization_id) as tenant:
                    backend = (
                        await tenant.session.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    async with stack.database.transaction() as observer:
                        assert (
                            await observer.execute(
                                text("SELECT pg_terminate_backend(:pid)"), {"pid": backend}
                            )
                        ).scalar_one()
                    await tenant.session.execute(text("SELECT 1"))
                raise AssertionError("terminated authority connection unexpectedly succeeded")
            raise errors[failure]()

    service = TelephonyService(
        stack.settings,
        stack.database,
        stack.event_platform.publisher,
        stack.vault,
        stack.transport,
        sip_permits=RejectingAuthority(),
    )
    request = CreateCallRequest(
        provider_account_id=account.id,
        from_number_id=number.id,
        destination="+14155550199",
        idempotency_key=uuid4().hex,
    )
    expected = (
        TelephonyAdmissionUnavailableError
        if failure in {"timeout", "database_disconnect"}
        else errors[failure]
    )
    with pytest.raises(expected):
        await service.create_call(organization.id, None, request)
    replay = await service.create_call(organization.id, None, request)
    assert replay.id == attempted[0] and replay.state == CallState.FAILED
    assert replay.error_code == "NXS_TELEPHONY_ADMISSION_FAILED"
    assert replay.provider_call_id is None
    assert len(attempted) == 1 and len(stack.transport.requests) == 0
    with pytest.raises(TelephonyIdempotencyConflictError):
        await service.create_call(
            organization.id, None, request.model_copy(update={"destination": "+14155550198"})
        )
    async with stack.database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM telephony_calls"))
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM event_outbox WHERE event_type='telephony.call.failed'")
            )
        ).scalar_one() == 1


@pytest.mark.parametrize("failure", ["missing_upstream", "suspended", "stale_generation"])
async def test_real_permit_authority_denial_records_failed_call(
    telephony_stack: Any,
    make_organization: Any,
    target_control: Any,
    failure: str,
    monkeypatch: Any,
) -> None:
    stack = telephony_stack
    cell, actor = target_control
    organization = await make_organization()
    account = await _account(stack, organization.id, provider=TelephonyProvider.ASTERISK)
    number = await _number(stack, organization.id, account)
    placement_service = CellPlacementService(stack.database, stack.event_platform.publisher)
    await placement_service.mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    original = await PlacementResolver(stack.database).resolve(organization.id)
    peer = PeerProfile(
        peer_id=uuid4(),
        direction="OUTBOUND",
        edge_ids=(uuid4(),),
        networks=("10.0.0.0/8",),
        transport="UDP",
        isolated_network=True,
        cell_id=cell,
    )
    permits = EgressPermits(stack.database, Fernet.generate_key(), PeerPolicy((peer,)))
    if failure != "missing_upstream":
        await PeerRegistry(stack.database).register(actor, peer, expected_revision=0)
        registry = UpstreamRegistry(stack.database)
        upstream = uuid4()
        await registry.register(
            actor,
            RegisterUpstream(
                id=upstream,
                revision=1,
                host="10.9.0.1",
                port=5060,
                transport="UDP",
                cell_id=cell,
                asterisk_peer_id=peer.peer_id,
            ),
        )
        await registry.bind_account(
            actor, organization.id, account.id, upstream, 1, expected_revision=0
        )
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
        if failure == "stale_generation":
            await placement_service.mutate(
                organization.id,
                actor,
                PlacementMutation(
                    cell_id=cell,
                    operation="RESUME",
                    expected_generation=2,
                    idempotency_key=uuid4().hex,
                    reason_code="TEST",
                ),
                uuid4(),
            )

            async def stale_snapshot(organization_id: Any) -> Any:
                assert organization_id == organization.id
                return original

            monkeypatch.setattr(permits._placement, "resolve", stale_snapshot)
    service = TelephonyService(
        stack.settings,
        stack.database,
        stack.event_platform.publisher,
        stack.vault,
        stack.transport,
        sip_permits=permits,
    )
    request = CreateCallRequest(
        provider_account_id=account.id,
        from_number_id=number.id,
        destination="+14155550199",
        idempotency_key=uuid4().hex,
    )
    with pytest.raises((SipRouteDeniedError, PlacementFencedError)):
        await service.create_call(organization.id, None, request)
    replay = await service.create_call(organization.id, None, request)
    assert replay.state == CallState.FAILED and replay.provider_call_id is None
    assert replay.error_code == "NXS_TELEPHONY_ADMISSION_FAILED"
    assert stack.transport.requests == []
    async with stack.database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_egress_permits"))
        ).scalar_one() == 0
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM telephony_calls"))
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM event_outbox WHERE event_type='telephony.call.failed'")
            )
        ).scalar_one() == 1
