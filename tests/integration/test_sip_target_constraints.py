"""Target constraints and explicit platform grants against real PostgreSQL."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.cells.contracts import RegisterCellRequest
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.contracts import Page, RegisterTarget, TargetMutation
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


async def test_target_rotation_replay_and_stale_control(
    target_control: Any, tenant_database: Any
) -> None:
    cell, actor = target_control
    registry = TargetRegistry(
        tenant_database, TargetNetworkPolicy(("10.0.0.0/8",), frozenset({5060}))
    )
    first = await registry.register(
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
    activation = TargetMutation(
        cell_id=cell,
        target_id=first.target_id,
        expected_revision=1,
        state="ACTIVE",
        idempotency_key=uuid4().hex,
        reason_code="TEST",
    )
    activated = await registry.transition(actor, activation, uuid4())
    assert activated.control_revision == 2
    second = await registry.register(
        actor,
        RegisterTarget(
            cell_id=cell,
            host="10.1.2.4",
            port=5060,
            transport="UDP",
            expected_revision=2,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    rotation = activation.model_copy(
        update={
            "target_id": second.target_id,
            "expected_revision": 3,
            "idempotency_key": uuid4().hex,
        }
    )
    rotated = await registry.transition(actor, rotation, uuid4())
    assert rotated.control_revision == 4
    assert await registry.transition(actor, rotation, uuid4()) == rotated
    assert await registry.transition(actor, activation, uuid4()) == activated
    assert (await registry.inspect(actor, cell, first.target_id)).state == "DRAINING"
    assert (await registry.inspect(actor, cell, second.target_id)).state == "ACTIVE"
    for changed in (
        rotation.model_copy(update={"expected_revision": 4}),
        rotation.model_copy(update={"idempotency_key": uuid4().hex}),
    ):
        with pytest.raises(SipConflictError):
            await registry.transition(actor, changed, uuid4())
    drained = await registry.transition(
        actor,
        TargetMutation(
            cell_id=cell,
            target_id=second.target_id,
            expected_revision=4,
            state="DRAINING",
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    assert drained.control_revision == 5
    assert len(await registry.list_targets(actor, cell, Page(limit=1))) == 1
    async with tenant_database.transaction() as session:
        assert (
            await session.execute(
                text("SELECT active_target_id FROM cell_sip_target_heads WHERE cell_id=:cell"),
                {"cell": cell},
            )
        ).scalar_one() is None
        assert (
            await session.execute(
                text("SELECT count(*) FROM sip_target_mutations WHERE cell_id=:cell"),
                {"cell": cell},
            )
        ).scalar_one() == 5


async def test_concurrent_target_activations_have_one_revision_winner(
    target_control: Any, tenant_database: Any
) -> None:
    cell, actor = target_control
    registry = TargetRegistry(
        tenant_database, TargetNetworkPolicy(("10.0.0.0/8",), frozenset({5060}))
    )
    targets = []
    for revision in range(2):
        targets.append(
            await registry.register(
                actor,
                RegisterTarget(
                    cell_id=cell,
                    host=f"10.1.2.{revision + 3}",
                    port=5060,
                    transport="UDP",
                    expected_revision=revision,
                    idempotency_key=uuid4().hex,
                    reason_code="TEST",
                ),
                uuid4(),
            )
        )
    barrier = asyncio.Barrier(2)

    async def activate(target_id: Any) -> Any:
        await barrier.wait()
        return await registry.transition(
            actor,
            TargetMutation(
                cell_id=cell,
                target_id=target_id,
                expected_revision=2,
                state="ACTIVE",
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )

    async with asyncio.timeout(10):
        results = await asyncio.gather(
            *(activate(target.target_id) for target in targets), return_exceptions=True
        )
    assert sum(isinstance(result, SipConflictError) for result in results) == 1
    async with tenant_database.transaction() as session:
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM cell_sip_targets WHERE cell_id=:cell AND state='ACTIVE'"
                ),
                {"cell": cell},
            )
        ).scalar_one() == 1
