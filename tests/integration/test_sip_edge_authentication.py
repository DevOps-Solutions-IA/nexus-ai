"""Durable request replay fencing, using independent PostgreSQL transactions."""

import asyncio
import hmac
import secrets
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from nexus_ai.sip_edge.authentication import EdgeAuthenticator
from nexus_ai.sip_edge.errors import SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.security import EdgeCredential, signing_payload

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def signed_request(database: Any, credential: EdgeCredential) -> dict[str, Any]:
    async with database.transaction() as session:
        timestamp = int(
            (await session.execute(select(func.clock_timestamp()))).scalar_one().timestamp()
        )
    request = dict(
        edge_id=credential.edge_id,
        boot_id=uuid4(),
        method="POST",
        path="/internal/sip/inbound",
        body=b"{}",
        timestamp=timestamp,
        nonce=secrets.token_hex(32),
    )
    request["signature"] = hmac.digest(
        credential.secret, signing_payload(**request), "sha256"
    ).hex()
    return request


async def test_authentication_replay_survives_service_recreation(tenant_database: Any) -> None:
    credential = EdgeCredential(uuid4(), secrets.token_bytes(32))
    request = await signed_request(tenant_database, credential)
    first = await EdgeAuthenticator(tenant_database, (credential,)).authenticate(**request)
    assert first.edge_id == credential.edge_id
    with pytest.raises(SipRouteDeniedError):
        await EdgeAuthenticator(tenant_database, (credential,)).authenticate(**request)


async def test_concurrent_signed_replay_has_one_winner(tenant_database: Any) -> None:
    credential = EdgeCredential(uuid4(), secrets.token_bytes(32))
    request = await signed_request(tenant_database, credential)
    barrier = asyncio.Barrier(2)

    async def authenticate() -> Any:
        await barrier.wait()
        return await EdgeAuthenticator(tenant_database, (credential,)).authenticate(**request)

    async with asyncio.timeout(10):
        results = await asyncio.gather(authenticate(), authenticate(), return_exceptions=True)
    assert sum(isinstance(result, SipRouteDeniedError) for result in results) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1


async def test_nonce_capacity_fails_closed_and_signature_does_not_reserve(
    tenant_database: Any,
) -> None:
    credential = EdgeCredential(uuid4(), secrets.token_bytes(32))
    authenticator = EdgeAuthenticator(tenant_database, (credential,), capacity=1)
    request = await signed_request(tenant_database, credential)
    with pytest.raises(SipRouteDeniedError):
        await authenticator.authenticate(**(request | {"signature": "0" * 64}))
    await authenticator.authenticate(**request)
    with pytest.raises(SipRouteUnavailableError):
        await authenticator.authenticate(**await signed_request(tenant_database, credential))
    async with tenant_database.transaction() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM sip_edge_replays"))
        ).scalar_one() == 0
        await session.execute(
            text("SELECT set_config('nxs.sip_edge_id', :edge, true)"),
            {"edge": str(credential.edge_id)},
        )
        assert (
            await session.execute(text("SELECT count(*) FROM sip_edge_replays"))
        ).scalar_one() == 1
        removed = await session.execute(text("DELETE FROM sip_edge_replays"))
        assert removed.rowcount == 0


async def test_changed_boot_cannot_reuse_nonce(tenant_database: Any) -> None:
    credential = EdgeCredential(uuid4(), secrets.token_bytes(32))
    request = await signed_request(tenant_database, credential)
    authenticator = EdgeAuthenticator(tenant_database, (credential,))
    await authenticator.authenticate(**request)
    changed = {key: value for key, value in request.items() if key != "signature"}
    changed["boot_id"] = uuid4()
    changed["signature"] = hmac.digest(
        credential.secret, signing_payload(**changed), "sha256"
    ).hex()
    with pytest.raises(SipRouteDeniedError):
        await authenticator.authenticate(**changed)
