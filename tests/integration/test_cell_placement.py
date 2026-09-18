"""P18 placement foundation exercised through PostgreSQL and the real P04 outbox."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from nexus_ai.cells.admission import PlacementAdmission, PlacementResolver
from nexus_ai.cells.contracts import (
    PlacementMutation,
    PlacementPage,
    RegisterCellRequest,
    RetireCellRequest,
)
from nexus_ai.cells.errors import (
    CellUnavailableError,
    PlacementConflictError,
    PlacementFencedError,
    PlacementRequiredError,
)
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.domain.cells.models import OrganizationPlacementRecord, PlacementMutationRecord

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def placement_setup(
    tenant_database: Any, event_platform: Any, make_organization: Any, make_tool_principal: Any
) -> Any:
    organization = await make_organization()
    actor = await make_tool_principal(organization)
    async with tenant_database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_grants (user_id, capability) VALUES (:actor, 'cell:control')"
            ),
            {"actor": actor.user_id},
        )
    service = CellPlacementService(tenant_database, event_platform.publisher)
    cell_id = await service.register(
        actor.user_id,
        RegisterCellRequest(
            cell_key=f"cell-{uuid4().hex}", idempotency_key=uuid4().hex, reason_code="TEST_REGISTER"
        ),
        uuid4(),
    )
    return organization, actor, service, cell_id


def _assign(cell_id: Any) -> PlacementMutation:
    return PlacementMutation(
        cell_id=cell_id, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST_ASSIGN"
    )


async def test_initial_assignment_replay_and_fingerprint_conflict(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    request = _assign(cell_id)
    first = await service.mutate(organization.id, actor.user_id, request, uuid4())
    assert first.assignment_generation == 1
    assert await service.mutate(organization.id, actor.user_id, request, uuid4()) == first
    with pytest.raises(PlacementConflictError):
        await service.mutate(
            organization.id,
            actor.user_id,
            request.model_copy(update={"reason_code": "DIFFERENT"}),
            uuid4(),
        )
    assert await PlacementResolver(tenant_database).resolve(organization.id) == first
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(select(func.count()).select_from(PlacementMutationRecord))
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox WHERE event_type = 'cell.assignment.created'"
                )
            )
        ).scalar_one() == 1


async def test_suspend_resume_never_revives_old_generation(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    guard = PlacementAdmission(cell_id)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await guard.admit(tenant, first)
    suspended = await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell_id,
            operation="SUSPEND",
            expected_generation=1,
            idempotency_key=uuid4().hex,
            reason_code="TEST_SUSPEND",
        ),
        uuid4(),
    )
    assert suspended.assignment_generation == 2
    with pytest.raises(PlacementFencedError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await guard.admit(tenant, first)
    current = await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell_id,
            operation="RESUME",
            expected_generation=2,
            idempotency_key=uuid4().hex,
            reason_code="TEST_RESUME",
        ),
        uuid4(),
    )
    assert current.assignment_generation == 3
    with pytest.raises(PlacementFencedError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await guard.admit(tenant, first)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await guard.admit(tenant, current)


async def test_missing_placement_and_wrong_worker_fail_closed(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    with pytest.raises(PlacementRequiredError):
        await PlacementResolver(tenant_database).resolve(organization.id)
    placed = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    with pytest.raises(PlacementFencedError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await PlacementAdmission(uuid4()).admit(tenant, placed)


async def test_cross_tenant_sql_and_snapshot_fence(
    placement_setup: Any, tenant_database: Any, make_organization: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    other = await make_organization()
    placed = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    async with tenant_database.tenant_transaction(other.id) as tenant:
        assert (
            await tenant.session.execute(
                text("SELECT id FROM organization_placements WHERE id = :id"),
                {"id": placed.placement_id},
            )
        ).first() is None
        assert (
            await tenant.session.execute(
                text("SELECT id FROM placement_mutations WHERE organization_id = :id"),
                {"id": organization.id},
            )
        ).first() is None
        with pytest.raises(PlacementFencedError):
            await PlacementAdmission(cell_id).admit(tenant, placed)


async def test_outbox_failure_rolls_back_placement_and_receipt(
    placement_setup: Any, tenant_database: Any, event_platform: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(event_platform.publisher, "enqueue", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (await tenant.session.execute(select(OrganizationPlacementRecord))).first() is None
        assert (await tenant.session.execute(select(PlacementMutationRecord))).first() is None


async def test_concurrent_equivalent_request_has_one_receipt(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    request = _assign(cell_id)
    barrier = asyncio.Barrier(2)

    async def assign() -> Any:
        await barrier.wait()
        return await service.mutate(organization.id, actor.user_id, request, uuid4())

    first, second = await asyncio.gather(assign(), assign())
    assert first == second
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(select(func.count()).select_from(PlacementMutationRecord))
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox WHERE event_type = 'cell.assignment.created'"
                )
            )
        ).scalar_one() == 1


async def test_history_is_immutable_in_postgres(placement_setup: Any, tenant_database: Any) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    with pytest.raises(DBAPIError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text("SELECT set_config('nxs.principal_id', :actor, true)"),
                {"actor": str(actor.user_id)},
            )
            await tenant.session.execute(
                text("UPDATE placement_mutations SET result_generation = 99")
            )


async def test_organization_owner_is_not_platform_control(
    tenant_database: Any, event_platform: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    actor = await make_tool_principal(organization)
    service = CellPlacementService(tenant_database, event_platform.publisher)
    with pytest.raises(PermissionDeniedError):
        await service.register(
            actor.user_id,
            RegisterCellRequest(
                cell_key="unauthorized", idempotency_key="unauthorized", reason_code="TEST"
            ),
            uuid4(),
        )


async def test_runtime_without_control_cannot_mutate_catalog_or_placement(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    placed = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    for statement, parameters in (
        ("UPDATE cells SET updated_at = clock_timestamp() WHERE id = :id", {"id": cell_id}),
        (
            "UPDATE organization_placements SET state = 'SUSPENDED', "
            "assignment_generation = 2 WHERE id = :id",
            {"id": placed.placement_id},
        ),
    ):
        with pytest.raises(DBAPIError):
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                await tenant.session.execute(text(statement), parameters)
    assert await PlacementResolver(tenant_database).resolve(organization.id) == placed


async def test_cross_cell_update_rejected_by_database(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    placed = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    another = await service.register(
        actor.user_id,
        RegisterCellRequest(
            cell_key=f"cell-{uuid4().hex}", idempotency_key=uuid4().hex, reason_code="TEST_REGISTER"
        ),
        uuid4(),
    )
    with pytest.raises(DBAPIError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text("SELECT set_config('nxs.principal_id', :actor, true)"),
                {"actor": str(actor.user_id)},
            )
            await tenant.session.execute(
                text(
                    "UPDATE organization_placements SET cell_id = :cell, state = 'SUSPENDED', "
                    "assignment_generation = 2 WHERE id = :id"
                ),
                {"cell": another, "id": placed.placement_id},
            )
    assert await PlacementResolver(tenant_database).resolve(organization.id) == placed


async def test_independent_first_assignments_have_one_winner(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    another = await service.register(
        actor.user_id,
        RegisterCellRequest(
            cell_key=f"cell-{uuid4().hex}", idempotency_key=uuid4().hex, reason_code="TEST_REGISTER"
        ),
        uuid4(),
    )
    barrier = asyncio.Barrier(2)

    async def assign(target: Any) -> Any:
        await barrier.wait()
        return await service.mutate(organization.id, actor.user_id, _assign(target), uuid4())

    results = await asyncio.gather(assign(cell_id), assign(another), return_exceptions=True)
    assert sum(isinstance(result, PlacementConflictError) for result in results) == 1
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(
                select(func.count()).select_from(OrganizationPlacementRecord)
            )
        ).scalar_one() == 1


async def test_admission_holds_authority_until_transaction_commit(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    placed = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    guard = PlacementAdmission(cell_id)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await guard.admit(tenant, placed)
        async with tenant_database.tenant_transaction(organization.id) as contender:
            with pytest.raises(DBAPIError) as error:
                async with contender.session.begin_nested():
                    await contender.session.execute(
                        text("SELECT id FROM cells WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": cell_id},
                    )
            assert error.value.orig.sqlstate == "55P03"


async def test_retirement_is_absorbing_and_replay_has_one_history(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    request = RetireCellRequest(cell_id=cell_id, idempotency_key=uuid4().hex, reason_code="RETIRE")
    first = await service.retire(actor.user_id, request, uuid4())
    assert first.state == "RETIRED"
    assert await service.retire(actor.user_id, request, uuid4()) == first
    with pytest.raises(PlacementConflictError):
        await service.retire(
            actor.user_id, request.model_copy(update={"reason_code": "OTHER"}), uuid4()
        )
    with pytest.raises(CellUnavailableError):
        await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    async with tenant_database.transaction() as session:
        await service._authorize(session, actor.user_id)
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM cell_control_history "
                    "WHERE cell_id = :cell AND operation = 'RETIRE'"
                ),
                {"cell": cell_id},
            )
        ).scalar_one() == 1
    with pytest.raises(DBAPIError):
        async with tenant_database.transaction() as session:
            await service._authorize(session, actor.user_id)
            await session.execute(
                text("UPDATE cells SET state = 'REGISTERED' WHERE id = :cell"), {"cell": cell_id}
            )


@pytest.mark.parametrize("suspended", [False, True])
async def test_bound_cell_cannot_retire(placement_setup: Any, suspended: bool) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    if suspended:
        await service.mutate(
            organization.id,
            actor.user_id,
            PlacementMutation(
                cell_id=cell_id,
                operation="SUSPEND",
                expected_generation=1,
                idempotency_key=uuid4().hex,
                reason_code="SUSPEND",
            ),
            uuid4(),
        )
    with pytest.raises(PlacementConflictError):
        await service.retire(
            actor.user_id,
            RetireCellRequest(cell_id=cell_id, idempotency_key=uuid4().hex, reason_code="RETIRE"),
            uuid4(),
        )


async def test_retirement_versus_assignment(placement_setup: Any, tenant_database: Any) -> None:
    organization, actor, service, cell_id = placement_setup
    barrier = asyncio.Barrier(2)

    async def retire() -> Any:
        await barrier.wait()
        return await service.retire(
            actor.user_id,
            RetireCellRequest(cell_id=cell_id, idempotency_key=uuid4().hex, reason_code="RETIRE"),
            uuid4(),
        )

    async def assign() -> Any:
        await barrier.wait()
        return await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())

    results = await asyncio.gather(retire(), assign(), return_exceptions=True)
    assert (
        sum(
            isinstance(result, (PlacementConflictError, CellUnavailableError)) for result in results
        )
        == 1
    )
    cell = await service.inspect_cell(actor.user_id, cell_id)
    if cell.state == "RETIRED":
        with pytest.raises(PlacementRequiredError):
            await PlacementResolver(tenant_database).resolve(organization.id)
    else:
        assert (
            await PlacementResolver(tenant_database).resolve(organization.id)
        ).cell_id == cell_id


async def test_catalog_administration_is_bounded_and_authorized(
    placement_setup: Any, make_tool_principal: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    reader = await make_tool_principal(organization)
    with pytest.raises(PermissionDeniedError):
        await service.inspect_cell(reader.user_id, cell_id)
    with pytest.raises(PermissionDeniedError):
        await service.list_cells(reader.user_id, PlacementPage())
    with pytest.raises(PermissionDeniedError):
        await service.retire(
            reader.user_id,
            RetireCellRequest(cell_id=cell_id, idempotency_key=uuid4().hex, reason_code="RETIRE"),
            uuid4(),
        )
    first = await service.list_cells(actor.user_id, PlacementPage(limit=1))
    assert len(first) == 1
    following = await service.list_cells(
        actor.user_id, PlacementPage(after_id=first[0].id, limit=1)
    )
    assert all(row.id > first[0].id for row in following)


async def test_composite_fk_and_distinct_uniqueness_contracts(
    placement_setup: Any, tenant_database: Any, make_organization: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    other = await make_organization()
    async with tenant_database.transaction() as session:
        definitions = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid IN ('organization_placements'::regclass, "
                        "'placement_mutations'::regclass)"
                    )
                )
            ).all()
        )
    assert definitions["uq_organization_placements_organization_id"] == "UNIQUE (organization_id)"
    assert definitions["uq_organization_placements_org_id"] == "UNIQUE (organization_id, id)"
    assert (
        "FOREIGN KEY (organization_id, placement_id)"
        in definitions["fk_placement_mutations_organization_id_organization_placements"]
    )
    with pytest.raises(DBAPIError) as failure:
        async with tenant_database.tenant_transaction(other.id) as tenant:
            await service._authorize(tenant.session, actor.user_id)
            tenant.session.add(
                PlacementMutationRecord(
                    id=uuid4(),
                    organization_id=other.id,
                    placement_id=first.placement_id,
                    cell_id=cell_id,
                    operation="ASSIGN",
                    idempotency_key_hash="a" * 64,
                    fingerprint="b" * 64,
                    expected_generation=None,
                    result_generation=1,
                    previous_state=None,
                    new_state="ACTIVE",
                    actor_user_id=actor.user_id,
                    reason_code="ATTACK",
                    correlation_id=uuid4(),
                )
            )
    assert failure.value.orig.sqlstate == "23503"


async def test_committed_response_loss_reconstructs_original_receipt(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    request = _assign(cell_id)

    async def lost_response() -> None:
        await service.mutate(organization.id, actor.user_id, request, uuid4())
        raise ConnectionError("injected response loss after committed transaction")

    with pytest.raises(ConnectionError, match="response loss"):
        await lost_response()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        receipt_id = (await tenant.session.execute(select(PlacementMutationRecord.id))).scalar_one()
    replay = await service.mutate(organization.id, actor.user_id, request, uuid4())
    assert replay.assignment_generation == 1
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(select(PlacementMutationRecord.id))
        ).scalars().all() == [receipt_id]
        assert (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox WHERE event_type = 'cell.assignment.created'"
                )
            )
        ).scalar_one() == 1


@pytest.mark.parametrize("operation,generation", [("SUSPEND", 1), ("RESUME", 2)])
async def test_concurrent_state_writers_change_generation_once(
    placement_setup: Any, tenant_database: Any, operation: str, generation: int
) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    if operation == "RESUME":
        await service.mutate(
            organization.id,
            actor.user_id,
            PlacementMutation(
                cell_id=cell_id,
                operation="SUSPEND",
                expected_generation=1,
                idempotency_key=uuid4().hex,
                reason_code="SUSPEND",
            ),
            uuid4(),
        )
    barrier = asyncio.Barrier(2)

    async def transition() -> Any:
        await barrier.wait()
        return await service.mutate(
            organization.id,
            actor.user_id,
            PlacementMutation(
                cell_id=cell_id,
                operation=operation,
                expected_generation=generation,
                idempotency_key=uuid4().hex,
                reason_code=operation,
            ),
            uuid4(),
        )

    results = await asyncio.gather(transition(), transition(), return_exceptions=True)
    assert sum(isinstance(result, PlacementConflictError) for result in results) == 1
    current = await PlacementResolver(tenant_database).resolve(organization.id)
    assert current.assignment_generation == generation + 1
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(select(func.count()).select_from(PlacementMutationRecord))
        ).scalar_one() == generation + 1


async def test_direct_duplicate_placement_rejected_by_postgres(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    with pytest.raises(DBAPIError) as failure:
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await service._authorize(tenant.session, actor.user_id)
            tenant.session.add(
                OrganizationPlacementRecord(
                    id=uuid4(),
                    organization_id=organization.id,
                    cell_id=cell_id,
                    assignment_generation=1,
                    state="ACTIVE",
                )
            )
    assert failure.value.orig.sqlstate == "23505"


async def test_restart_reconstructs_from_postgres_without_cache_or_bus(
    placement_setup: Any, tenant_database: Any
) -> None:
    from nexus_ai.infrastructure.database import Database

    organization, actor, service, cell_id = placement_setup
    expected = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    replacement = Database(tenant_database._settings, worker_cell_id=cell_id)
    await replacement.connect()
    try:
        assert await PlacementResolver(replacement).resolve(organization.id) == expected
        async with replacement.execution_transaction(organization.id):
            pass
        await replacement.disconnect()
        await replacement.connect()
        assert await PlacementResolver(replacement).resolve(organization.id) == expected
    finally:
        await replacement.disconnect()


async def test_runtime_rls_cannot_be_disabled_and_foreign_updates_change_zero_rows(
    placement_setup: Any, tenant_database: Any, make_organization: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    other = await make_organization()
    report = await tenant_database.runtime_role_report()
    assert report.role == "nexus_runtime"
    assert not report.can_bypass_tenancy
    async with tenant_database.tenant_transaction(other.id) as tenant:
        await service._authorize(tenant.session, actor.user_id)
        result = await tenant.session.execute(
            text(
                "UPDATE organization_placements SET state = 'SUSPENDED', assignment_generation = 2 "
                "WHERE id = :id RETURNING id"
            ),
            {"id": first.placement_id},
        )
        assert result.all() == []
    with pytest.raises(DBAPIError):
        async with tenant_database.tenant_transaction(other.id) as tenant:
            await tenant.session.execute(text("SET LOCAL row_security = off"))
            await tenant.session.execute(text("SELECT * FROM organization_placements"))
    assert await PlacementResolver(tenant_database).resolve(organization.id) == first


async def test_event_replay_and_forged_projection_never_grant_authority(
    placement_setup: Any, tenant_database: Any
) -> None:
    from nexus_ai.cells.contracts import PlacementState
    from nexus_ai.events.envelope import EventEnvelope
    from nexus_ai.events.errors import UnsupportedEventVersionError
    from nexus_ai.events.registry import EVENT_REGISTRY

    organization, actor, service, cell_id = placement_setup
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    suspended = await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell_id,
            operation="SUSPEND",
            expected_generation=1,
            idempotency_key=uuid4().hex,
            reason_code="SUSPEND",
        ),
        uuid4(),
    )
    for generation in (2, 1, 1, 2, 1):
        envelope = EventEnvelope.create(
            event_type="cell.assignment.created",
            event_version=1,
            aggregate_type="cell_assignment",
            aggregate_id=str(first.placement_id),
            producer="nexus-ai",
            organization_id=organization.id,
            payload={
                "placement_id": str(first.placement_id),
                "cell_id": str(cell_id),
                "assignment_generation": generation,
                "state": "ACTIVE",
                "reason_code": "REPLAY",
            },
        )
        EVENT_REGISTRY.decode(envelope)
        assert await PlacementResolver(tenant_database).resolve(organization.id) == suspended
        with pytest.raises(PlacementFencedError):
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                await PlacementAdmission(cell_id).admit(
                    tenant,
                    first.model_copy(
                        update={"assignment_generation": generation, "state": PlacementState.ACTIVE}
                    ),
                )
    with pytest.raises(UnsupportedEventVersionError):
        EVENT_REGISTRY.model_for("cell.assignment.created", 999)


@pytest.mark.parametrize("changed", ["cell_id", "expected_generation"])
async def test_transition_key_conflicts_on_target_or_generation_change(
    placement_setup: Any, tenant_database: Any, changed: str
) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    another = await service.register(
        actor.user_id,
        RegisterCellRequest(
            cell_key=f"cell-{uuid4().hex}", idempotency_key=uuid4().hex, reason_code="REGISTER"
        ),
        uuid4(),
    )
    request = PlacementMutation(
        cell_id=cell_id,
        operation="SUSPEND",
        expected_generation=1,
        idempotency_key=uuid4().hex,
        reason_code="SUSPEND",
    )
    expected = await service.mutate(organization.id, actor.user_id, request, uuid4())
    conflicting = request.model_copy(update={changed: another if changed == "cell_id" else 2})
    with pytest.raises(PlacementConflictError):
        await service.mutate(organization.id, actor.user_id, conflicting, uuid4())
    assert await PlacementResolver(tenant_database).resolve(organization.id) == expected


async def test_resolution_races_suspension_but_snapshot_never_authorizes(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    resolved = asyncio.Event()
    suspended = asyncio.Event()

    async def delayed_delivery() -> None:
        expected = await PlacementResolver(tenant_database).resolve(organization.id)
        resolved.set()
        await suspended.wait()
        with pytest.raises(PlacementFencedError):
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                await PlacementAdmission(cell_id).admit(tenant, expected)

    async def suspend() -> None:
        await resolved.wait()
        await service.mutate(
            organization.id,
            actor.user_id,
            PlacementMutation(
                cell_id=cell_id,
                operation="SUSPEND",
                expected_generation=1,
                idempotency_key=uuid4().hex,
                reason_code="SUSPEND",
            ),
            uuid4(),
        )
        suspended.set()

    async with asyncio.timeout(10):
        await asyncio.gather(delayed_delivery(), suspend())


async def test_disconnected_database_never_uses_last_route(
    placement_setup: Any, tenant_database: Any
) -> None:
    from nexus_ai.cells.admission import placement_route
    from nexus_ai.core.errors import DependencyUnavailableError
    from nexus_ai.infrastructure.database import Database

    organization, actor, service, cell_id = placement_setup
    expected = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    disconnected = Database(tenant_database._settings, worker_cell_id=cell_id)
    with placement_route(expected), pytest.raises(DependencyUnavailableError):
        async with disconnected.execution_transaction(organization.id):
            pytest.fail("cached route authorized without PostgreSQL")
    with pytest.raises(DependencyUnavailableError):
        await PlacementResolver(disconnected).resolve(organization.id)


async def test_bus_outage_does_not_change_placement_authority(
    placement_setup: Any, tenant_database: Any, nats_messaging: Any, integration_env: Any
) -> None:
    from nexus_ai.infrastructure.cache import Cache

    organization, actor, service, cell_id = placement_setup
    expected = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    cache = Cache(integration_env().cache)
    await cache.connect()
    key = f"nxs:p18:test:{organization.id}"
    await cache.client.set(key, expected.model_dump_json(), ex=10)
    await cache.client.delete(key)
    await cache.disconnect()
    await nats_messaging.disconnect()
    assert await PlacementResolver(tenant_database).resolve(organization.id) == expected
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await PlacementAdmission(cell_id).admit(tenant, expected)


async def test_suspended_placement_cannot_be_relocated(
    placement_setup: Any, tenant_database: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    suspended = await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell_id,
            operation="SUSPEND",
            expected_generation=1,
            idempotency_key=uuid4().hex,
            reason_code="SUSPEND",
        ),
        uuid4(),
    )
    another = await service.register(
        actor.user_id,
        RegisterCellRequest(
            cell_key=f"cell-{uuid4().hex}", idempotency_key=uuid4().hex, reason_code="REGISTER"
        ),
        uuid4(),
    )
    with pytest.raises(PlacementConflictError):
        await service.mutate(
            organization.id,
            actor.user_id,
            PlacementMutation(
                cell_id=another,
                operation="RESUME",
                expected_generation=2,
                idempotency_key=uuid4().hex,
                reason_code="RESUME",
            ),
            uuid4(),
        )
    with pytest.raises(DBAPIError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await service._authorize(tenant.session, actor.user_id)
            await tenant.session.execute(
                text(
                    "UPDATE organization_placements SET cell_id = :cell, state = 'ACTIVE', "
                    "assignment_generation = 3 WHERE id = :id"
                ),
                {"cell": another, "id": suspended.placement_id},
            )
    assert await PlacementResolver(tenant_database).resolve(organization.id) == suspended


@pytest.mark.parametrize("retired", [False, True])
async def test_unknown_or_retired_worker_never_grants_admission(
    placement_setup: Any, tenant_database: Any, retired: bool
) -> None:
    organization, actor, service, cell_id = placement_setup
    placed = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    other = uuid4()
    if retired:
        other = await service.register(
            actor.user_id,
            RegisterCellRequest(
                cell_key=f"retired-{uuid4().hex}",
                idempotency_key=uuid4().hex,
                reason_code="REGISTER",
            ),
            uuid4(),
        )
        await service.retire(
            actor.user_id,
            RetireCellRequest(cell_id=other, idempotency_key=uuid4().hex, reason_code="RETIRE"),
            uuid4(),
        )
    with pytest.raises(CellUnavailableError) as failure:
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await PlacementAdmission(other).admit(
                tenant, placed.model_copy(update={"cell_id": other})
            )
    assert str(other) not in str(failure.value)
    assert str(organization.id) not in str(failure.value)
