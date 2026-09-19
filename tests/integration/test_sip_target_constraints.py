"""Target constraints and explicit platform grants against real PostgreSQL."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.cells.contracts import RegisterCellRequest
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.contracts import RegisterTarget
from nexus_ai.sip_edge.errors import SipConflictError
from nexus_ai.sip_edge.targets import TargetNetworkPolicy, TargetRegistry

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def target_control(
    tenant_database: Any, event_platform: Any, make_organization: Any, make_tool_principal: Any
) -> Any:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    async with tenant_database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_grants (user_id, capability) VALUES "
                "(:actor, 'cell:control'), (:actor, 'sip_edge:control')"
            ),
            {"actor": principal.user_id},
        )
    cell = await CellPlacementService(tenant_database, event_platform.publisher).register(
        principal.user_id,
        RegisterCellRequest(
            cell_key=f"sip-{uuid4().hex}", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    return cell, principal.user_id


async def insert_target(session: Any, cell: Any, revision: int, host: str = "10.1.2.3") -> Any:
    identity = uuid4()
    await session.execute(
        text(
            "INSERT INTO cell_sip_targets "
            "(id,cell_id,target_revision,host,port,transport,state) "
            "VALUES (:id,:cell,:revision,:host,5060,'UDP','REGISTERED')"
        ),
        {"id": identity, "cell": cell, "revision": revision, "host": host},
    )
    return identity


async def authenticate(session: Any, actor: Any) -> None:
    await session.execute(
        text("SELECT set_config('nxs.principal_id', :actor, true)"), {"actor": str(actor)}
    )


async def test_target_write_requires_explicit_platform_grant(
    target_control: Any, tenant_database: Any
) -> None:
    cell, _ = target_control
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await insert_target(session, cell, 1)


async def test_target_tuple_is_immutable_and_active_is_unique(
    target_control: Any, tenant_database: Any
) -> None:
    cell, actor = target_control
    async with tenant_database.transaction() as session:
        await authenticate(session, actor)
        first = await insert_target(session, cell, 1)
        second = await insert_target(session, cell, 2)
        await session.execute(
            text("UPDATE cell_sip_targets SET state='ACTIVE' WHERE id=:id"), {"id": first}
        )
    for query in (
        "UPDATE cell_sip_targets SET state='ACTIVE' WHERE id=:id",
        "UPDATE cell_sip_targets SET host='10.2.3.4',state='RETIRED' WHERE id=:id",
        "UPDATE cell_sip_targets SET target_revision=3,state='RETIRED' WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            async with tenant_database.transaction() as session:
                await authenticate(session, actor)
                await session.execute(text(query), {"id": second})
    async with tenant_database.transaction() as session:
        await authenticate(session, actor)
        await session.execute(
            text("UPDATE cell_sip_targets SET state='DRAINING' WHERE id=:id"), {"id": first}
        )
        await session.execute(
            text("UPDATE cell_sip_targets SET state='ACTIVE' WHERE id=:id"), {"id": second}
        )
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM cell_sip_targets WHERE cell_id=:cell AND state='ACTIVE'"
                ),
                {"cell": cell},
            )
        ).scalar_one() == 1


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "sip:x@10.0.0.1"])
async def test_database_rejects_unsafe_target(
    target_control: Any, tenant_database: Any, host: str
) -> None:
    cell, actor = target_control
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await authenticate(session, actor)
            await insert_target(session, cell, 1, host)


async def test_retired_target_cannot_resurrect(target_control: Any, tenant_database: Any) -> None:
    cell, actor = target_control
    async with tenant_database.transaction() as session:
        await authenticate(session, actor)
        identity = await insert_target(session, cell, 1)
        await session.execute(
            text("UPDATE cell_sip_targets SET state='RETIRED' WHERE id=:id"), {"id": identity}
        )
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await authenticate(session, actor)
            await session.execute(
                text("UPDATE cell_sip_targets SET state='ACTIVE' WHERE id=:id"), {"id": identity}
            )


async def test_registration_concurrent_same_key_has_one_receipt(
    target_control: Any, tenant_database: Any
) -> None:
    cell, actor = target_control
    registry = TargetRegistry(
        tenant_database, TargetNetworkPolicy(("10.0.0.0/8",), frozenset({5060}))
    )
    request = RegisterTarget(
        cell_id=cell,
        host="10.1.2.3",
        port=5060,
        transport="UDP",
        expected_revision=0,
        idempotency_key=uuid4().hex,
        reason_code="TEST",
    )
    barrier = asyncio.Barrier(2)

    async def register() -> Any:
        await barrier.wait()
        return await registry.register(actor, request, uuid4())

    async with asyncio.timeout(10):
        first, second = await asyncio.gather(register(), register())
    assert first == second
    async with tenant_database.transaction() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM sip_target_mutations WHERE cell_id=:cell"),
                {"cell": cell},
            )
        ).scalar_one() == 1
    with pytest.raises(SipConflictError):
        await registry.register(actor, request.model_copy(update={"host": "10.2.3.4"}), uuid4())
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await authenticate(session, actor)
            await session.execute(
                text("UPDATE cell_sip_target_heads SET control_revision=0 WHERE cell_id=:cell"),
                {"cell": cell},
            )
