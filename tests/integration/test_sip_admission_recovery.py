"""Durable pre-ARI fencing, including an entirely unavailable disposable database."""

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.domain.organizations.entities import OrganizationDraft
from nexus_ai.domain.organizations.service import OrganizationService
from nexus_ai.domain.telephony.repository import TelephonySecretStore
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerPolicy, PeerProfile
from nexus_ai.sip_edge.upstreams import RegisterUpstream, UpstreamRegistry
from nexus_ai.telephony import admission
from nexus_ai.telephony.entities import CallState, CreateCallRequest, TelephonyProvider
from nexus_ai.telephony.providers.base import TransportError
from nexus_ai.telephony.service import (
    AmbiguousProviderTimeoutError,
    TelephonyAdmissionUnavailableError,
    TelephonyService,
)
from tests.conftest import MIGRATION_DSN, RUNTIME_DSN, SUPERUSER_DSN, FakeTelephonyTransport
from tests.integration.test_sip_target_constraints import target_control as target_control
from tests.integration.test_telephony_service import _account, _number

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class OwnerDied(BaseException):
    pass


async def expire_admission(database_name: str, call_id: Any) -> None:
    connection = await asyncpg.connect(
        make_url(SUPERUSER_DSN).set(database=database_name).render_as_string(hide_password=False)
    )
    try:
        async with connection.transaction():
            await connection.execute("SET LOCAL session_replication_role = replica")
            await connection.execute(
                "UPDATE sip_call_admissions "
                "SET created_at=clock_timestamp()-interval '31 seconds', "
                "expires_at=clock_timestamp()-interval '1 second' WHERE call_id=$1",
                call_id,
            )
    finally:
        await connection.close()


@pytest.fixture
async def admission_stack(telephony_stack: Any, make_organization: Any, target_control: Any) -> Any:
    stack = telephony_stack
    cell, actor = target_control
    organization = await make_organization()
    account = await _account(stack, organization.id, provider=TelephonyProvider.ASTERISK)
    await stack.service.update_account(
        organization.id, account.id, {"ari_base": "https://asterisk.test/ari"}
    )
    number = await _number(stack, organization.id, account)
    await CellPlacementService(stack.database, stack.event_platform.publisher).mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    peer = PeerProfile(
        peer_id=uuid4(),
        direction="OUTBOUND",
        edge_ids=(uuid4(),),
        networks=("10.0.0.0/8",),
        transport="UDP",
        isolated_network=True,
        cell_id=cell,
    )
    await PeerRegistry(stack.database).register(actor, peer, expected_revision=0)
    upstream = uuid4()
    registry = UpstreamRegistry(stack.database)
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
    permits = EgressPermits(stack.database, Fernet.generate_key(), PeerPolicy((peer,)))
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
    return SimpleNamespace(
        stack=stack, organization=organization.id, permits=permits, service=service, request=request
    )


@pytest.mark.parametrize("after_permit", [False, True])
@pytest.mark.parametrize("idempotent", [False, True])
async def test_owner_crash_recovery_fences_old_owner(
    admission_stack: Any, monkeypatch: Any, after_permit: bool, idempotent: bool
) -> None:
    env = admission_stack
    request = (
        env.request if idempotent else env.request.model_copy(update={"idempotency_key": None})
    )
    original = env.permits.issue_for_call
    call_ids = []

    async def crash(organization_id: Any, call_id: Any, account_id: Any) -> Any:
        call_ids.append(call_id)
        if after_permit:
            await original(organization_id, call_id, account_id)
        raise OwnerDied()

    monkeypatch.setattr(env.permits, "issue_for_call", crash)
    with pytest.raises(OwnerDied):
        await env.service.create_call(env.organization, None, request)
    call_id = call_ids[0]
    before = await env.service.get_call(env.organization, call_id)
    assert before.state == CallState.CREATED
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        owner = (
            await tenant.session.execute(text("SELECT owner_id FROM sip_call_admissions"))
        ).scalar_one()
    await expire_admission(make_url(RUNTIME_DSN).database, call_id)
    assert await env.service.recover_pending_admissions(env.organization) == 1
    assert await env.service.recover_pending_admissions(env.organization) == 0
    recovered = await env.service.get_call(env.organization, call_id)
    assert recovered.state == CallState.FAILED
    assert recovered.error_code == "NXS_TELEPHONY_ADMISSION_FAILED"
    with pytest.raises(SipRouteDeniedError):
        await original(env.organization, call_id, request.provider_account_id)
    if idempotent:
        assert (await env.service.create_call(env.organization, None, request)).id == call_id
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        with pytest.raises(SipRouteDeniedError):
            await admission.fence(tenant, call_id, owner_id=owner, dispatch=True)
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM telephony_calls"))
        ).scalar_one() == 1
        states = (
            (await tenant.session.execute(text("SELECT state FROM sip_egress_permits")))
            .scalars()
            .all()
        )
        assert states == (["REVOKED"] if after_permit else [])
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM event_outbox WHERE event_type='telephony.call.failed'")
            )
        ).scalar_one() == 1
    assert env.stack.transport.requests == []


async def test_admission_owner_rls_and_database_fences(
    admission_stack: Any, monkeypatch: Any, make_organization: Any
) -> None:
    env = admission_stack
    calls = []

    async def stop(*arguments: Any) -> Any:
        calls.append(arguments[1])
        raise OwnerDied()

    monkeypatch.setattr(env.permits, "issue_for_call", stop)
    with pytest.raises(OwnerDied):
        await env.service.create_call(env.organization, None, env.request)
    call_id = calls[0]
    foreign = await make_organization()
    async with env.stack.database.tenant_transaction(foreign.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_call_admissions"))
        ).scalar_one() == 0
        assert (
            await tenant.session.execute(text("UPDATE sip_call_admissions SET state='REVOKED'"))
        ).rowcount == 0
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        with pytest.raises(SipRouteDeniedError):
            await admission.fence(tenant, call_id, owner_id=uuid4(), dispatch=True)
        owner_id = (
            await tenant.session.execute(text("SELECT owner_id FROM sip_call_admissions"))
        ).scalar_one()
        with pytest.raises(SipRouteDeniedError):
            await admission.fence(tenant, call_id, owner_id=owner_id, dispatch=True)
    for mutation in (
        "UPDATE sip_call_admissions SET owner_id=gen_random_uuid()",
        "UPDATE sip_call_admissions SET expires_at=expires_at+interval '1 second'",
        "UPDATE sip_call_admissions SET state='DISPATCHED'",
        "UPDATE sip_call_admissions SET state='UNKNOWN'",
    ):
        with pytest.raises(IntegrityError):
            async with env.stack.database.tenant_transaction(env.organization) as tenant:
                await tenant.session.execute(text(mutation))
    with pytest.raises(ValueError):
        await env.service.recover_pending_admissions(env.organization, limit=101)


async def test_admission_recovery_and_outbox_are_atomic(
    admission_stack: Any, monkeypatch: Any
) -> None:
    env = admission_stack
    calls = []

    async def stop(*arguments: Any) -> Any:
        calls.append(arguments[1])
        raise OwnerDied()

    monkeypatch.setattr(env.permits, "issue_for_call", stop)
    with pytest.raises(OwnerDied):
        await env.service.create_call(env.organization, None, env.request)
    await expire_admission(make_url(RUNTIME_DSN).database, calls[0])
    original = env.service._enqueue_state_event

    async def reject_outbox(*arguments: Any) -> Any:
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(env.service, "_enqueue_state_event", reject_outbox)
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        await env.service.recover_pending_admissions(env.organization)
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        assert (
            await tenant.session.execute(text("SELECT state FROM sip_call_admissions"))
        ).scalar_one() == "PENDING"
        assert (
            await tenant.session.execute(text("SELECT state FROM telephony_calls"))
        ).scalar_one() == "CREATED"
    monkeypatch.setattr(env.service, "_enqueue_state_event", original)
    assert await env.service.recover_pending_admissions(env.organization) == 1


async def test_concurrent_replay_does_not_revoke_active_owner(
    admission_stack: Any, monkeypatch: Any
) -> None:
    env = admission_stack
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.permits.issue_for_call

    async def paused(*arguments: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*arguments)

    monkeypatch.setattr(env.permits, "issue_for_call", paused)
    owner = asyncio.create_task(env.service.create_call(env.organization, None, env.request))
    async with asyncio.timeout(10):
        await entered.wait()
        replay = await env.service.create_call(env.organization, None, env.request)
        assert replay.state == CallState.CREATED and replay.error_code is None
        assert await env.service.recover_pending_admissions(env.organization) == 0
        release.set()
        result = await owner
    assert result.id == replay.id and result.provider_call_id is not None
    assert len(env.stack.transport.requests) == 1
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM sip_egress_permits"))
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(text("SELECT state FROM sip_call_admissions"))
        ).scalar_one() == "DISPATCHED"


async def test_recovery_wins_before_dispatch_and_old_owner_cannot_send(
    admission_stack: Any, monkeypatch: Any
) -> None:
    env = admission_stack
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.service._resolve_secret

    async def paused(*arguments: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*arguments)

    monkeypatch.setattr(env.service, "_resolve_secret", paused)
    owner = asyncio.create_task(env.service.create_call(env.organization, None, env.request))
    async with asyncio.timeout(10):
        await entered.wait()
        replay = await env.service.create_call(env.organization, None, env.request)
        await expire_admission(make_url(RUNTIME_DSN).database, replay.id)
        assert await env.service.recover_pending_admissions(env.organization) == 1
        release.set()
        with pytest.raises(SipRouteDeniedError):
            await owner
    assert env.stack.transport.requests == []
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        assert (
            await tenant.session.execute(text("SELECT state FROM sip_egress_permits"))
        ).scalar_one() == "REVOKED"
        assert (
            await tenant.session.execute(text("SELECT state FROM telephony_calls"))
        ).scalar_one() == "FAILED"


@pytest.mark.parametrize("crash", [False, True])
async def test_after_dispatch_never_reoriginates(admission_stack: Any, crash: bool) -> None:
    env = admission_stack

    def lose_response(request: Any) -> Any:
        if crash:
            raise OwnerDied()
        return TransportError("lost response", timeout=True)

    env.stack.transport.set_handler(lose_response)
    with pytest.raises(OwnerDied if crash else AmbiguousProviderTimeoutError):
        await env.service.create_call(env.organization, None, env.request)
    replay = await env.service.create_call(env.organization, None, env.request)
    await expire_admission(make_url(RUNTIME_DSN).database, replay.id)
    assert await env.service.recover_pending_admissions(env.organization) == 0
    assert replay.state == CallState.CREATED and replay.error_code in {
        "NXS_TELEPHONY_PROVIDER_TIMEOUT",
        "NXS_TELEPHONY_PROVIDER_DISPATCH_UNCONFIRMED",
    }
    assert len(env.stack.transport.requests) == 1


async def test_provider_link_preserves_concurrent_terminal_error(admission_stack: Any) -> None:
    env = admission_stack

    async def terminal_callback(request: Any) -> Any:
        async with env.stack.database.tenant_transaction(env.organization) as tenant:
            call_id = (
                await tenant.session.execute(text("SELECT id FROM telephony_calls"))
            ).scalar_one()
        await env.service._mark_failed(env.organization, call_id)
        return 200, {"id": "terminal-channel"}

    env.stack.transport.set_handler(terminal_callback)
    call = await env.service.create_call(env.organization, None, env.request)
    assert call.state == CallState.FAILED
    assert call.error_code == "NXS_TELEPHONY_PROVIDER_ERROR"
    assert call.provider_call_id == "terminal-channel"


@pytest.mark.parametrize("terminal_call", [False, True])
async def test_preexisting_revocation_and_terminal_call_recovery(
    admission_stack: Any, monkeypatch: Any, terminal_call: bool
) -> None:
    env = admission_stack
    calls = []
    original = env.permits.issue_for_call

    async def crash(*arguments: Any) -> Any:
        calls.append(arguments[1])
        await original(*arguments)
        raise OwnerDied()

    monkeypatch.setattr(env.permits, "issue_for_call", crash)
    with pytest.raises(OwnerDied):
        await env.service.create_call(env.organization, None, env.request)
    call_id = calls[0]
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        await tenant.session.execute(text("UPDATE sip_egress_permits SET state='REVOKED'"))
    if terminal_call:
        await env.service._mark_failed(env.organization, call_id)
    await expire_admission(make_url(RUNTIME_DSN).database, call_id)
    assert await env.service.recover_pending_admissions(env.organization) == 1
    recovered = await env.service.get_call(env.organization, call_id)
    assert recovered.state == CallState.FAILED and env.stack.transport.requests == []
    async with env.stack.database.tenant_transaction(env.organization) as tenant:
        assert (
            await tenant.session.execute(text("SELECT state FROM sip_call_admissions"))
        ).scalar_one() == "REVOKED"
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM event_outbox WHERE event_type='telephony.call.failed'")
            )
        ).scalar_one() == 1


@pytest.mark.parametrize("idempotent", [True, False])
async def test_total_database_unavailability_and_recovery(
    telephony_stack: Any, integration_env: Any, idempotent: bool
) -> None:
    name = "nxs_admission_outage_" + uuid4().hex
    control = await asyncpg.connect(SUPERUSER_DSN)
    await control.execute(f'CREATE DATABASE "{name}" OWNER nexus_migration')
    runtime_dsn = make_url(RUNTIME_DSN).set(database=name).render_as_string(hide_password=False)
    environment = dict(os.environ) | {
        "NXS_ENVIRONMENT": "test",
        "NXS_DATABASE__DSN": runtime_dsn,
        "NXS_DATABASE__MIGRATION_DSN": make_url(MIGRATION_DSN)
        .set(database=name)
        .render_as_string(hide_password=False),
    }
    database = Database(integration_env(NXS_DATABASE__DSN=runtime_dsn).database)
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "alembic",
            "upgrade",
            "head",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()[-2000:]
        await database.connect()
        vault = LocalEncryptedVault(
            TelephonySecretStore(database), build_fernet([Fernet.generate_key().decode()])
        )
        settings = integration_env(NXS_DATABASE__DSN=runtime_dsn)
        transport = FakeTelephonyTransport()
        publisher = telephony_stack.event_platform.publisher
        service = TelephonyService(settings, database, publisher, vault, transport)
        stack = SimpleNamespace(service=service)
        organization = await OrganizationService(database).create_core_record(
            OrganizationDraft(
                organization_key="outage-" + uuid4().hex,
                display_name="Outage",
                legal_name="Outage",
                country_code="US",
                timezone="UTC",
            )
        )
        account = await _account(stack, organization.id, provider=TelephonyProvider.ASTERISK)
        number = await _number(stack, organization.id, account)
        calls = []

        class UnavailableAuthority:
            async def issue_for_call(
                self, organization_id: Any, call_id: Any, account_id: Any
            ) -> Any:
                calls.append(call_id)
                async with database.tenant_transaction(organization_id) as tenant:
                    assert (
                        await tenant.session.execute(text("SELECT state FROM sip_call_admissions"))
                    ).scalar_one() == "PENDING"
                await control.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
                await control.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", name
                )
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    await asyncpg.connect(
                        make_url(runtime_dsn)
                        .set(drivername="postgresql")
                        .render_as_string(hide_password=False)
                    )
                async with database.tenant_transaction(organization_id):
                    raise AssertionError("unavailable database allowed authority")

        service = TelephonyService(
            settings, database, publisher, vault, transport, sip_permits=UnavailableAuthority()
        )
        request = CreateCallRequest(
            provider_account_id=account.id,
            from_number_id=number.id,
            destination="+14155550199",
            idempotency_key=uuid4().hex if idempotent else None,
        )
        with pytest.raises(TelephonyAdmissionUnavailableError):
            await service.create_call(organization.id, None, request)
        assert transport.requests == []
        assert not await control.fetchval(
            "SELECT datallowconn FROM pg_database WHERE datname=$1", name
        )
        await control.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        await expire_admission(name, calls[0])
        if idempotent:
            recovered = await service.create_call(organization.id, None, request)
        else:
            recovered = (await service.list_calls(organization.id, account_id=None, limit=10))[0]
        assert recovered.id == calls[0] and recovered.state == CallState.FAILED
        assert recovered.provider_call_id is None and transport.requests == []
        assert await service.recover_pending_admissions(organization.id) == 0
        async with database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(text("SELECT count(*) FROM telephony_calls"))
            ).scalar_one() == 1
            assert (
                await tenant.session.execute(text("SELECT count(*) FROM sip_egress_permits"))
            ).scalar_one() == 0
            assert (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM event_outbox WHERE event_type='telephony.call.failed'"
                    )
                )
            ).scalar_one() == 1
    finally:
        await database.disconnect()
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await control.close()
