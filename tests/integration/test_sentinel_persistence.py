"""Real PostgreSQL authority, isolation and deterministic concurrency proof."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, false, func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from nexus_ai.domain.sentinel.models import (
    SentinelActionProposal,
    SentinelApproval,
    SentinelIncident,
    SentinelSignalReceipt,
)
from nexus_ai.infrastructure.orm import Base
from nexus_ai.infrastructure.schema_guard import tenant_tables
from nexus_ai.sentinel.config import SentinelSettings
from nexus_ai.sentinel.contracts import (
    Approval,
    Facts,
    Finding,
    IncidentState,
    Risk,
    Signal,
    Subject,
    digest,
    proposal_fingerprint,
)
from nexus_ai.sentinel.database import SentinelDatabase
from nexus_ai.sentinel.errors import SentinelConflict, SentinelDenied
from nexus_ai.sentinel.service import SentinelStore, SourceBinding
from tests.conftest import RUNTIME_DSN, SUPERUSER_DSN
from tests.unit.test_sentinel_foundation import book, proposal

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SENTINEL_DSN = (
    make_url(RUNTIME_DSN)
    .set(username="nexus_sentinel", password="local-sentinel-only")
    .render_as_string(hide_password=False)
)


@pytest.fixture
async def database(migrated_database):
    admin = await asyncpg.connect(SUPERUSER_DSN)
    await admin.execute(
        "TRUNCATE sentinel_executions, sentinel_approvals, sentinel_action_proposals, "
        "sentinel_findings, sentinel_signal_receipts, sentinel_incidents, sentinel_runbooks, "
        "sentinel_control_state"
    )
    await admin.execute(
        "INSERT INTO sentinel_control_state (id, revision, mutable_actions_enabled) "
        "VALUES (1,1,false)"
    )
    await admin.close()
    database = SentinelDatabase(
        SentinelSettings(
            enabled=True,
            database_dsn=SecretStr(SENTINEL_DSN),
            mutable_actions_enabled=True,
        )
    )
    try:
        yield database
    finally:
        await database.close()


def signal(**overrides):
    return Signal.model_validate(
        {
            "adapter_id": "foundation",
            "adapter_revision": 1,
            "source_kind": "health",
            "source_identity": "test-service",
            "source_observation_id": str(uuid7()),
            "observed_at": datetime.now(UTC),
            "subject_kind": "SERVICE",
            "subject_id": uuid7(),
            "severity": "WARNING",
            "fingerprint": digest("dependency"),
            "facts": {"condition": "DEGRADED"},
            **overrides,
        }
    )


def store(database, signals, books=()):
    bindings = {
        SourceBinding(
            item.adapter_id,
            item.adapter_revision,
            item.source_kind,
            item.source_identity,
            item.subject_kind,
            item.subject_id,
            item.organization_id,
        )
        for item in signals
    }
    return SentinelStore(database, sources=bindings, runbooks=books)


def coordinate_transactions(monkeypatch, database, parties=2):
    original = database.transaction
    barrier = asyncio.Barrier(parties)

    @asynccontextmanager
    async def transaction():
        async with original() as session:
            async with asyncio.timeout(10):
                await barrier.wait()
            yield session

    monkeypatch.setattr(database, "transaction", transaction)


async def test_role_properties_and_platform_rls(database):
    async with database.transaction() as session:
        row = (
            await session.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
                    "rolreplication, has_schema_privilege(current_user, 'public', 'CREATE') "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert row[0] == "nexus_sentinel" and not any(row[1:])
        assert await session.scalar(
            text("SELECT current_setting('nxs.organization_id', true)")
        ) in {None, ""}
        rows = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname LIKE 'sentinel_%' AND relkind = 'r'"
                )
            )
        ).all()
        assert len(rows) == 8 and all(all(row) for row in rows)
        assert await session.scalar(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname=current_user")
        )
        for table, _ in tenant_tables():
            assert not await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user,:name,'SELECT,INSERT,UPDATE,DELETE')"
                ),
                {"name": table.name},
            )
    for sql in ("CREATE TABLE unauthorized_probe (id int)", "SET ROLE nexus_runtime"):
        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                await session.execute(text(sql))


async def test_wrong_actual_database_role_fails_closed(migrated_database):
    settings = SentinelSettings(enabled=True, database_dsn=SecretStr(SENTINEL_DSN))
    database = SentinelDatabase(
        settings.model_copy(update={"database_dsn": SecretStr(RUNTIME_DSN)})
    )
    try:
        with pytest.raises(SentinelDenied, match="unsafe_sentinel_database_identity"):
            async with database.transaction():
                pytest.fail("wrong identity admitted")
    finally:
        await database.close()


@pytest.mark.parametrize(
    "table",
    [
        "organizations",
        "memberships",
        "conversations",
        "tool_execution_records",
        "ai_agent_sessions",
        "ai_agent_turns",
        "organization_placements",
    ],
)
@pytest.mark.parametrize("operation", ["SELECT", "INSERT", "UPDATE", "DELETE"])
async def test_tenant_table_denial(database, table, operation):
    target = Base.metadata.tables[table]
    statements = {
        "SELECT": select(target).limit(1),
        "INSERT": insert(target).values(id=uuid7()),
        "UPDATE": update(target).values(id=target.c.id).where(false()),
        "DELETE": delete(target).where(false()),
    }
    with pytest.raises(DBAPIError) as error:
        async with database.transaction() as session:
            await session.execute(
                text("SELECT set_config('nxs.organization_id', :org, true)"), {"org": str(uuid7())}
            )
            await session.execute(statements[operation])
    assert error.value.orig.sqlstate == "42501"


async def test_runtime_cannot_access_sentinel(database):
    connection = await asyncpg.connect(RUNTIME_DSN.replace("+asyncpg", ""))
    try:
        for table in (
            "sentinel_signal_receipts",
            "sentinel_incidents",
            "sentinel_findings",
            "sentinel_runbooks",
            "sentinel_action_proposals",
            "sentinel_approvals",
            "sentinel_executions",
            "sentinel_control_state",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch(str(select(Base.metadata.tables[table])))
    finally:
        await connection.close()


async def test_duplicate_signal_converges(database, monkeypatch):
    item = signal()
    service = store(database, [item])
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(service.ingest(item), service.ingest(item))
    assert results[0] == results[1]
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(SentinelSignalReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(SentinelIncident)) == 1
    assert await service.ingest(item) == results[0]
    with pytest.raises(SentinelConflict):
        await service.ingest(item.model_copy(update={"facts": Facts(condition="HEALTHY")}))


async def test_concurrent_correlation(database, monkeypatch):
    first = signal()
    second = first.model_copy(update={"source_observation_id": str(uuid7())})
    service = store(database, [first, second])
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(service.ingest(first), service.ingest(second))
    assert results[0][0] != results[1][0] and results[0][1] == results[1][1]
    async with database.transaction() as session:
        assert (await session.get(SentinelIncident, results[0][1])).revision == 2


async def test_subjects_do_not_collide_and_metadata_is_not_authority(database):
    identity = uuid7()
    items = [
        signal(
            subject_kind=kind, subject_id=identity, source_identity=kind, organization_id=uuid7()
        )
        for kind in Subject
    ]
    service = store(database, items)
    results = [await service.ingest(item) for item in items]
    assert len({result[1] for result in results}) == 4
    with pytest.raises(SentinelDenied):
        await service.ingest(items[0].model_copy(update={"organization_id": uuid7()}))
    with pytest.raises(SentinelDenied):
        await SentinelStore(database).ingest(items[0])
    async with database.transaction() as session:
        assert await session.scalar(
            text("SELECT current_setting('nxs.organization_id', true)")
        ) in {None, ""}


async def test_incident_transition_race_and_database_guard(database, monkeypatch):
    item = signal()
    service = store(database, [item])
    _, identity = await service.ingest(item)
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(
            service.transition(identity, 1, IncidentState.TRIAGED),
            service.transition(identity, 1, IncidentState.CLOSED),
            return_exceptions=True,
        )
    assert results[0] == 2 and isinstance(results[1], (SentinelDenied, SentinelConflict))
    with pytest.raises(DBAPIError):
        async with database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE sentinel_incidents SET state='CLOSED', revision=revision+1 WHERE id=:id"
                ),
                {"id": identity},
            )
    revision = 2
    for state in (
        IncidentState.MITIGATION_PROPOSED,
        IncidentState.MITIGATING,
        IncidentState.MONITORING,
    ):
        revision = await service.transition(identity, revision, state)
    with pytest.raises(SentinelDenied):
        await service.transition(identity, revision, IncidentState.RESOLVED)
    revision = await service.transition(
        identity, revision, IncidentState.RESOLVED, resolution_source="AUTHORIZED_OPERATOR"
    )
    await service.transition(identity, revision, IncidentState.CLOSED)


async def setup_proposal(database, risk=Risk.OBSERVE):
    item = signal()
    definition = book(risk)
    service = store(database, [item], [definition])
    _, incident = await service.ingest(item)
    await service.register_runbook(definition)
    request = proposal(
        incident_id=incident, runbook_id=definition.id, target_id=item.subject_id, risk=risk
    )
    return service, definition, request


async def test_runbook_immutable_new_revision(database):
    service, definition, request = await setup_proposal(database)
    assert await service.register_runbook(definition) == definition.id
    for sql in (
        "UPDATE sentinel_runbooks SET revision=2 WHERE id=:id",
        "DELETE FROM sentinel_runbooks WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                await session.execute(text(sql), {"id": definition.id})
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="immutable Sentinel"):
            await admin.execute(
                "UPDATE sentinel_runbooks SET revision=2 WHERE id=$1", definition.id
            )
    finally:
        await admin.close()
    changed = definition.model_copy(update={"id": uuid7(), "revision": 2})
    updated_store = SentinelStore(database, runbooks=[definition, changed])
    await updated_store.register_runbook(changed)
    with pytest.raises(SentinelDenied, match="superseded_runbook"):
        await updated_store.propose(request)
    with pytest.raises(SentinelDenied):
        await service.register_runbook(changed)
    collision = definition.model_copy(update={"timeout_seconds": 11})
    with pytest.raises(SentinelConflict):
        await SentinelStore(database, runbooks=[collision]).register_runbook(collision)


async def test_equivalent_proposals_converge(database, monkeypatch):
    service, _, request = await setup_proposal(database)
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(service.propose(request), service.propose(request))
    assert results[0] == results[1]
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(SentinelActionProposal)) == 1
    assert await service.eligible(results[0], target_generation=1)
    assert not await service.eligible(results[0], target_generation=2)


async def test_durable_approval_policy_and_kill_switch(database):
    service, _, request = await setup_proposal(database, Risk.REVERSIBLE)
    identity = await service.propose(request)
    assert not await service.eligible(identity, target_generation=1)
    decision = Approval(
        proposal_id=identity,
        proposal_fingerprint=proposal_fingerprint(request),
        approver_principal=uuid7(),
        decision="APPROVED",
        reason_code="operator_review",
        policy_revision=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert await service.record_approval(decision) == await service.record_approval(decision)
    assert not await service.eligible(identity, target_generation=1)
    assert await service.set_mutable_actions(1, enabled=True) == 2
    assert not await service.eligible(identity, target_generation=1)
    with pytest.raises(SentinelConflict):
        await service.set_mutable_actions(1, enabled=True)
    current = request.model_copy(update={"policy_revision": 2})
    current_id = await service.propose(current)
    assert not await service.eligible(current_id, target_generation=1)
    current_approval = decision.model_copy(
        update={
            "proposal_id": current_id,
            "policy_revision": 2,
            "proposal_fingerprint": proposal_fingerprint(current),
        }
    )
    await service.record_approval(current_approval)
    assert await service.eligible(current_id, target_generation=1)
    await service.set_mutable_actions(2, enabled=False)
    assert not await service.eligible(current_id, target_generation=1)
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(SentinelApproval)) == 2


async def test_findings_bounded_and_append_only(database):
    service, _, request = await setup_proposal(database)
    service.settings = service.settings.model_copy(update={"max_findings_per_incident": 1})
    async with database.transaction() as session:
        receipt_id = await session.scalar(
            select(SentinelSignalReceipt.id).where(
                SentinelSignalReceipt.incident_id == request.incident_id
            )
        )
    finding = Finding(
        incident_id=request.incident_id,
        hypothesis_category="dependency",
        explanation="Safe structured hypothesis",
        confidence=0.5,
        evidence_refs=(receipt_id,),
    )
    identity = await service.add_finding(finding)
    with pytest.raises(SentinelDenied, match="finding_budget"):
        await service.add_finding(finding)
    with pytest.raises(DBAPIError):
        async with database.transaction() as session:
            await session.execute(
                text("DELETE FROM sentinel_findings WHERE id=:id"), {"id": identity}
            )


async def test_boundaries_and_unknown_identities(database):
    service, _, request = await setup_proposal(database)
    assert not await service.eligible(uuid7(), target_generation=None)
    with pytest.raises(SentinelDenied):
        await service.transition(uuid7(), 1, IncidentState.TRIAGED)
    for changed in (
        request.model_copy(update={"incident_revision": 2}),
        request.model_copy(update={"target_id": uuid7()}),
        request.model_copy(update={"policy_revision": 2}),
        request.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}),
    ):
        with pytest.raises(SentinelDenied):
            await service.propose(changed)
    assert request.incident_id in await service.list_incidents(limit=1)
    assert await service.list_incidents(after=request.incident_id, limit=1) == []
    with pytest.raises(SentinelDenied):
        await service.list_incidents(limit=100000)
    service.settings = service.settings.model_copy(update={"max_proposals_per_incident": 1})
    await service.propose(request)
    with pytest.raises(SentinelDenied, match="proposal_budget"):
        await service.propose(request.model_copy(update={"operation_identity": uuid7()}))


async def test_execution_foundation_does_not_grant_dispatch(database):
    with pytest.raises(DBAPIError) as error:
        async with database.transaction() as session:
            await session.execute(
                text("INSERT INTO sentinel_executions (id) VALUES (:id)"), {"id": uuid7()}
            )
    assert error.value.orig.sqlstate == "42501"


async def test_inherited_role_and_tenant_context_fail_closed(database):
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await admin.execute("GRANT nexus_runtime TO nexus_sentinel")
        with pytest.raises(SentinelDenied):
            async with database.transaction():
                pytest.fail("inherited tenant authority was admitted")
    finally:
        await admin.execute("REVOKE nexus_runtime FROM nexus_sentinel")
        await admin.close()
    async with database.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('nxs.organization_id', :id, false)"), {"id": str(uuid7())}
        )
        await connection.commit()
    with pytest.raises(SentinelDenied):
        async with database.transaction():
            pytest.fail("contaminated tenant connection was admitted")


async def test_evidence_and_approval_tampering_denied(database):
    service, _, request = await setup_proposal(database)
    with pytest.raises(SentinelDenied, match="evidence_not_bound"):
        await service.add_finding(
            Finding(
                incident_id=request.incident_id,
                hypothesis_category="dependency",
                explanation="Unknown evidence",
                confidence=0.5,
                evidence_refs=(uuid7(),),
            )
        )
    identity = await service.propose(request)
    decision = Approval(
        proposal_id=identity,
        proposal_fingerprint=proposal_fingerprint(request),
        approver_principal=uuid7(),
        decision="APPROVED",
        reason_code="reviewed",
        policy_revision=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    for invalid in (
        decision.model_copy(update={"proposal_id": uuid7()}),
        decision.model_copy(update={"proposal_fingerprint": "0" * 64}),
        decision.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}),
    ):
        with pytest.raises(SentinelDenied):
            await service.record_approval(invalid)
    await service.record_approval(decision)
    with pytest.raises(SentinelConflict):
        await service.record_approval(decision.model_copy(update={"decision": "REJECTED"}))
    denied = decision.model_copy(update={"approver_principal": uuid7(), "decision": "REJECTED"})
    await service.record_approval(denied)
    assert not await service.eligible(identity, target_generation=1)


async def test_signal_rollback_is_atomic(database, monkeypatch):
    item = signal()
    service = store(database, [item])
    original = database.transaction

    @asynccontextmanager
    async def failing_transaction():
        async with original() as session:
            yield session
            raise SentinelDenied("injected_pre_commit_failure")

    with monkeypatch.context() as patch:
        patch.setattr(database, "transaction", failing_transaction)
        with pytest.raises(SentinelDenied):
            await service.ingest(item)
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(SentinelSignalReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(SentinelIncident)) == 0
    await service.ingest(item)


async def test_policy_cas_race_has_one_winner(database, monkeypatch):
    service = SentinelStore(database)
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(
            service.set_mutable_actions(1, enabled=True),
            service.set_mutable_actions(1, enabled=False),
            return_exceptions=True,
        )
    assert results.count(2) == 1
    assert sum(isinstance(result, SentinelConflict) for result in results) == 1


async def test_database_rejects_rls_disable_and_record_tampering(database):
    item = signal()
    service = store(database, [item])
    receipt_id, incident_id = await service.ingest(item)
    statements = (
        ("UPDATE sentinel_signal_receipts SET payload='{}'::jsonb WHERE id=:id", receipt_id),
        ("UPDATE sentinel_incidents SET revision=revision+2 WHERE id=:id", incident_id),
        ("UPDATE sentinel_control_state SET revision=99 WHERE id=1", None),
    )
    for sql, identity in statements:
        with pytest.raises(DBAPIError) as error:
            async with database.transaction() as session:
                await session.execute(text(sql), {"id": identity})
        assert error.value.orig.sqlstate == "23514"
    with pytest.raises(DBAPIError) as error:
        async with database.transaction() as session:
            await session.execute(text("SET LOCAL row_security = off"))
            await session.execute(select(SentinelIncident))
    assert error.value.orig.sqlstate == "42501"
