"""PostgreSQL linearization, crash windows and final dispatch authorization."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid7

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from nexus_ai.domain.sentinel.models import (
    SentinelActionProposal,
    SentinelApproval,
    SentinelExecution,
    SentinelFinding,
    SentinelIncident,
)
from nexus_ai.sentinel.contracts import (
    Approval,
    Facts,
    Finding,
    IncidentState,
    Parameters,
    Risk,
    proposal_fingerprint,
)
from nexus_ai.sentinel.database import SentinelDatabase
from nexus_ai.sentinel.errors import SentinelConflict, SentinelDenied
from nexus_ai.sentinel.execution import ExecutionClaim, SentinelActionExecutor
from nexus_ai.sentinel.runbooks import (
    ActionResult,
    HandlerBinding,
    ProvenPreEffectRejection,
    SentinelRunbookRegistry,
)
from tests.conftest import MIGRATION_DSN, RUNTIME_DSN, SUPERUSER_DSN
from tests.integration.test_sentinel_persistence import (
    coordinate_transactions,
    signal,
    store,
)
from tests.integration.test_sentinel_persistence import (
    database as database,
)
from tests.unit.test_sentinel_foundation import book, proposal

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class Adapter:
    def __init__(self, binding):
        self.binding = binding
        self.execute = AsyncMock(return_value=ActionResult(classification="OBSERVED"))


async def pipeline(database, risk=Risk.OBSERVE, *, approve=True):
    item = signal()
    binding = HandlerBinding(
        "test.observe",
        1,
        risk,
        risk != Risk.REVERSIBLE,
        ((item.subject_kind, item.subject_id, 1),),
    )
    adapter = Adapter(binding)
    definition = book(risk, handler_key=binding.key, handler_binding_digest=binding.fingerprint)
    service = store(database, [item], [definition])
    receipt, incident = await service.ingest(item)
    await service.register_runbook(definition)
    revision = 1
    if risk == Risk.REVERSIBLE:
        revision = await service.set_mutable_actions(1, enabled=True)
    request = proposal(
        incident_id=incident,
        runbook_id=definition.id,
        target_id=item.subject_id,
        risk=risk,
        policy_revision=revision,
    )
    identity = await service.propose(request)
    if risk == Risk.REVERSIBLE and approve:
        await service.record_approval(
            Approval(
                proposal_id=identity,
                proposal_fingerprint=proposal_fingerprint(request),
                approver_principal=uuid7(),
                decision="APPROVED",
                reason_code="reviewed",
                policy_revision=revision,
                expires_at=request.expires_at,
            )
        )
    executor = SentinelActionExecutor(service, SentinelRunbookRegistry((adapter,)))
    return executor, adapter, identity, request, item, receipt


async def test_authority_login_failure_never_dispatches(database):
    executor, adapter, identity, *_ = await pipeline(database)
    await database.close()
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await admin.execute("ALTER ROLE nexus_sentinel NOLOGIN")
        with pytest.raises(Exception, match="not permitted to log in"):
            await executor.claim(identity, uuid7())
        adapter.execute.assert_not_awaited()
    finally:
        await admin.execute("ALTER ROLE nexus_sentinel LOGIN")
        await admin.close()
    claim = await executor.claim(identity, uuid7())
    assert await executor.dispatch(claim) == "SUCCEEDED"
    assert adapter.execute.await_count == 1


async def test_c20_total_database_outage_never_dispatches(database):
    name = "nxs_p20_outage_" + uuid7().hex
    admin = await asyncpg.connect(SUPERUSER_DSN)
    await admin.execute(f'CREATE DATABASE "{name}" OWNER nexus_migration')
    isolated = SentinelDatabase(
        database.settings.model_copy(
            update={
                "database_dsn": SecretStr(
                    make_url(database.settings.database_dsn.get_secret_value())
                    .set(database=name)
                    .render_as_string(hide_password=False)
                )
            }
        )
    )
    environment = dict(os.environ) | {
        "NXS_ENVIRONMENT": "test",
        "NXS_DATABASE__DSN": make_url(RUNTIME_DSN)
        .set(database=name)
        .render_as_string(hide_password=False),
        "NXS_DATABASE__MIGRATION_DSN": make_url(MIGRATION_DSN)
        .set(database=name)
        .render_as_string(hide_password=False),
    }
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
        async with asyncio.timeout(120):
            output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()[-2000:]
        executor, adapter, identity, *_ = await pipeline(isolated)
        await isolated.close()
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", name
        )
        dsn = (
            make_url(RUNTIME_DSN)
            .set(database=name, drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await asyncpg.connect(dsn)
        with pytest.raises(Exception, match="not currently accepting connections"):
            await executor.claim(identity, uuid7())
        adapter.execute.assert_not_awaited()
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        claim = await executor.claim(identity, uuid7())
        assert await executor.dispatch(claim) == "SUCCEEDED"
        assert adapter.execute.await_count == 1
    finally:
        await isolated.close()
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def test_c22_expired_durable_approval_denies_dispatch(database):
    executor, adapter, identity, request, *_ = await pipeline(
        database, Risk.REVERSIBLE, approve=False
    )
    async with database.transaction() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        session.add(
            SentinelApproval(
                id=uuid7(),
                proposal_id=identity,
                proposal_fingerprint=proposal_fingerprint(request),
                approver_principal=uuid7(),
                decision="APPROVED",
                reason_code="historical_approval",
                policy_revision=request.policy_revision,
                issued_at=now - timedelta(minutes=2),
                expires_at=now - timedelta(minutes=1),
            )
        )
    claim = await expired_claim(executor, identity)
    with pytest.raises(SentinelDenied, match="approval"):
        await executor.dispatch(claim)
    adapter.execute.assert_not_awaited()
    async with database.transaction() as session:
        assert (await session.get(SentinelActionProposal, identity)).state == "FAILED"


async def test_c22_approval_expires_between_claim_and_dispatch(database, monkeypatch):
    executor, adapter, identity, request, *_ = await pipeline(
        database, Risk.REVERSIBLE, approve=False
    )
    async with database.transaction() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        deadline = now + timedelta(seconds=1)
    await executor.store.record_approval(
        Approval(
            proposal_id=identity,
            proposal_fingerprint=proposal_fingerprint(request),
            approver_principal=uuid7(),
            decision="APPROVED",
            reason_code="bounded",
            policy_revision=request.policy_revision,
            expires_at=deadline,
        )
    )
    claim = await executor.claim(identity, uuid7())
    entered, release = asyncio.Event(), asyncio.Event()
    original = database.transaction

    @asynccontextmanager
    async def gated_transaction():
        entered.set()
        await release.wait()
        async with original() as session:
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(database, "transaction", gated_transaction)
        task = asyncio.create_task(executor.dispatch(claim))
        await entered.wait()
        async with asyncio.timeout(5), original() as session:
            while await session.scalar(select(func.clock_timestamp())) < deadline:
                pass
        release.set()
        with pytest.raises(SentinelDenied, match="approval"):
            await task
    adapter.execute.assert_not_awaited()


async def test_c18_c19_tenant_engines_unavailable_do_not_become_platform_authority(
    database, monkeypatch
):
    from nexus_ai.agents.service import AgentService
    from nexus_ai.tools.service import ToolEngine

    unavailable = AsyncMock(side_effect=RuntimeError("tenant engine unavailable"))
    monkeypatch.setattr(ToolEngine, "invoke", unavailable)
    monkeypatch.setattr(AgentService, "create_account", unavailable)
    executor, adapter, identity, *_ = await pipeline(database)
    claim = await executor.claim(identity, uuid7())
    assert await executor.dispatch(claim) == "SUCCEEDED"
    assert adapter.execute.await_count == 1
    unavailable.assert_not_awaited()


async def test_native_postgres_nats_and_nxs_probes(database, integration_env):
    from pathlib import Path

    from nexus_ai.core.health import HealthStatus
    from nexus_ai.infrastructure.messaging import Messaging
    from nexus_ai.sentinel.adapters import JetStreamHealthProbe, PostgreSQLHealthProbe
    from nexus_ai.sentinel.signals import NxsStateProbe

    messaging = Messaging(integration_env().messaging)
    await messaging.connect()
    try:
        assert (await PostgreSQLHealthProbe(database)()).status == HealthStatus.UP
        assert (await JetStreamHealthProbe(messaging)()).status == HealthStatus.UP
        assert (await NxsStateProbe(Path.cwd())()).status == HealthStatus.UP
    finally:
        await messaging.disconnect()


async def expired_claim(executor, identity, *, future_seconds=None):
    async with executor.store.database.transaction() as session:
        proposal_row = await executor._lock_proposal(session, identity)
        now = await session.scalar(select(func.clock_timestamp()))
        execution = SentinelExecution(
            id=uuid7(),
            proposal_id=identity,
            execution_generation=1,
            owner_id=uuid7(),
            started_at=now - timedelta(minutes=2),
            lease_expires_at=now + timedelta(seconds=future_seconds)
            if future_seconds is not None
            else now - timedelta(minutes=1),
            dispatch_state="CLAIMED",
            idempotency_key=f"sentinel:{identity}",
        )
        session.add(execution)
        proposal_row.state = "EXECUTING"
        await session.flush()
        return ExecutionClaim(
            identity, execution.id, execution.owner_id, 1, execution.idempotency_key
        )


async def test_c05_concurrent_findings_are_append_only(database, monkeypatch):
    executor, _, _, request, _, receipt = await pipeline(database)
    finding = Finding(
        incident_id=request.incident_id,
        hypothesis_category="dependency",
        explanation="Advisory",
        confidence=0.5,
        evidence_refs=(receipt,),
    )
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        identities = await asyncio.gather(
            executor.store.add_finding(finding), executor.store.add_finding(finding)
        )
    assert identities[0] != identities[1]
    async with database.transaction() as session:
        assert sorted((await session.scalars(select(SentinelFinding.revision))).all()) == [1, 2]


async def test_c11_concurrent_claim_has_one_owner(database, monkeypatch):
    executor, adapter, identity, *_ = await pipeline(database)
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(
            executor.claim(identity, uuid7()),
            executor.claim(identity, uuid7()),
            return_exceptions=True,
        )
    winners = [value for value in results if isinstance(value, ExecutionClaim)]
    assert len(winners) == 1
    assert sum(isinstance(value, SentinelConflict) for value in results) == 1
    assert await executor.dispatch(winners[0]) == "SUCCEEDED"
    assert adapter.execute.await_count == 1
    assert adapter.execute.call_args.args[0].idempotency_key == winners[0].idempotency_key
    with pytest.raises(SentinelDenied):
        await executor.claim(identity, uuid7())


async def test_global_execution_budget_serializes_different_incidents(database, monkeypatch):
    first, _, first_id, *_ = await pipeline(database)
    second, _, second_id, *_ = await pipeline(database)
    for executor in (first, second):
        executor.store.settings = executor.store.settings.model_copy(
            update={"max_active_executions": 1}
        )
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(
            first.claim(first_id, uuid7()), second.claim(second_id, uuid7()), return_exceptions=True
        )
    assert sum(isinstance(value, ExecutionClaim) for value in results) == 1
    assert sum(isinstance(value, SentinelDenied) for value in results) == 1


async def test_external_dispatch_holds_no_sentinel_transaction(database):
    executor, adapter, identity, *_ = await pipeline(database)

    async def external_effect(request):
        async with asyncio.timeout(2), database.transaction() as session:
            row = await session.scalar(
                select(SentinelActionProposal)
                .where(SentinelActionProposal.id == identity)
                .with_for_update()
            )
            assert row.state == "EXECUTING"
        return ActionResult(classification="OBSERVED")

    adapter.execute.side_effect = external_effect
    claim = await executor.claim(identity, uuid7())
    assert await executor.dispatch(claim) == "SUCCEEDED"


async def test_c12_c13_expired_owner_fenced_by_generation(database):
    executor, adapter, identity, *_ = await pipeline(database)
    old = await expired_claim(executor, identity)
    with pytest.raises(SentinelDenied, match="lease"):
        await executor.prepare_dispatch(old)
    recovered = await executor.claim(identity, uuid7())
    assert recovered.generation == old.generation + 1
    assert recovered.idempotency_key == old.idempotency_key
    with pytest.raises(SentinelDenied, match="stale_execution_owner"):
        await executor.prepare_dispatch(old)
    assert await executor.dispatch(recovered) == "SUCCEEDED"
    assert adapter.execute.await_count == 1


async def test_c14_crash_before_dispatch_cleanup_is_terminal(database):
    executor, adapter, identity, *_ = await pipeline(database)
    claim = await expired_claim(executor, identity)
    assert await executor.cleanup() == 1
    assert await executor.cleanup() == 0
    with pytest.raises(SentinelDenied):
        await executor.dispatch(claim)
    with pytest.raises(SentinelDenied):
        await executor.claim(identity, uuid7())
    adapter.execute.assert_not_awaited()
    async with database.transaction() as session:
        assert (await session.get(SentinelActionProposal, identity)).state == "FAILED"


async def test_c15_crash_after_dispatch_never_reclaims(database):
    executor, adapter, identity, *_ = await pipeline(database)
    claim = await executor.claim(identity, uuid7())
    await executor.prepare_dispatch(claim)
    with pytest.raises(SentinelDenied, match="already_fenced"):
        await executor.claim(identity, uuid7())
    await executor.finish(claim, "AMBIGUOUS")
    with pytest.raises(SentinelDenied):
        await executor.dispatch(claim)
    adapter.execute.assert_not_awaited()


async def test_c15_cleanup_after_dispatch_crash_persists_ambiguity(database):
    executor, adapter, identity, *_ = await pipeline(database)
    claim = await expired_claim(executor, identity, future_seconds=3)
    await executor.prepare_dispatch(claim)
    async with asyncio.timeout(10), database.transaction() as session:
        row = await session.get(SentinelExecution, claim.execution_id)
        while await session.scalar(select(func.clock_timestamp())) < row.lease_expires_at:
            pass
    assert await executor.cleanup() == 1
    async with database.transaction() as session:
        row = await session.get(SentinelExecution, claim.execution_id)
        assert row.dispatch_state == "AMBIGUOUS"
        assert (await session.get(SentinelActionProposal, identity)).state == "AMBIGUOUS"
    with pytest.raises(SentinelDenied):
        await executor.claim(identity, uuid7())
    adapter.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,outcome",
    [
        (TimeoutError(), "AMBIGUOUS"),
        (RuntimeError("unsafe upstream detail"), "AMBIGUOUS"),
        (ProvenPreEffectRejection(), "FAILED"),
    ],
)
async def test_c16_c17_effect_failure_classification(database, failure, outcome):
    executor, adapter, identity, *_ = await pipeline(database)
    adapter.execute.side_effect = failure
    claim = await executor.claim(identity, uuid7())
    assert await executor.dispatch(claim) == outcome
    async with database.transaction() as session:
        record = await session.get(SentinelExecution, claim.execution_id)
        assert record.result_classification == outcome
        assert record.error_classification is None
    with pytest.raises(SentinelDenied):
        await executor.claim(identity, uuid7())
    assert adapter.execute.await_count == 1


@pytest.mark.parametrize("change", ["proposal", "runbook", "policy", "target"])
async def test_c07_c08_c09_c10_final_semantic_fence(database, change):
    executor, adapter, identity, request, item, _ = await pipeline(database, Risk.REVERSIBLE)
    claim = await executor.claim(identity, uuid7())
    if change == "proposal":
        altered = request.model_copy(update={"parameters": Parameters(sample_limit=2)})
        replacement = await executor.store.propose(altered)
        with pytest.raises(SentinelDenied, match="approval"):
            await executor.claim(replacement, uuid7())
        return
    if change == "runbook":
        original = executor.store.runbooks[request.runbook_id]
        revised = original.model_copy(update={"id": uuid7(), "revision": 2})
        other = store(database, [item], [revised])
        await other.register_runbook(revised)
    elif change == "policy":
        await executor.store.set_mutable_actions(2, enabled=False)
    else:
        adapter.binding = HandlerBinding(
            adapter.binding.key,
            1,
            Risk.REVERSIBLE,
            False,
            ((item.subject_kind, item.subject_id, 2),),
        )
    with pytest.raises(SentinelDenied):
        await executor.prepare_dispatch(claim)
    adapter.execute.assert_not_awaited()


async def test_c21_kill_switch_serializes_with_dispatch(database, monkeypatch):
    executor, adapter, identity, *_ = await pipeline(database, Risk.REVERSIBLE)
    claim = await executor.claim(identity, uuid7())
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(
            executor.prepare_dispatch(claim),
            executor.store.set_mutable_actions(2, enabled=False),
            return_exceptions=True,
        )
    assert results[1] == 3
    assert isinstance(results[0], (tuple, SentinelDenied))
    with pytest.raises(SentinelDenied):
        await executor.prepare_dispatch(claim)
    adapter.execute.assert_not_awaited()


async def test_c23_resolution_and_new_observation(database, monkeypatch):
    executor, _, _, request, item, _ = await pipeline(database)
    service = executor.store
    revision = 1
    for state in list(IncidentState)[1:5]:
        revision = await service.transition(request.incident_id, revision, state)
    next_signal = item.model_copy(update={"source_observation_id": str(uuid7())})
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(
            service.transition(
                request.incident_id,
                revision,
                IncidentState.RESOLVED,
                resolution_source="AUTHORIZED_OPERATOR",
            ),
            service.ingest(next_signal),
            return_exceptions=True,
        )
    assert all(isinstance(value, (int, tuple, SentinelConflict)) for value in results)
    if isinstance(results[1], SentinelConflict):
        results[1] = await service.ingest(next_signal)
    async with database.transaction() as session:
        current = await session.get(SentinelIncident, results[1][1])
        assert current.state not in {"RESOLVED", "CLOSED"}


async def test_trusted_recovery_requires_latest_healthy_receipt(database):
    executor, _, _, request, item, receipt = await pipeline(database)
    service = executor.store
    revision = 1
    for state in list(IncidentState)[1:5]:
        revision = await service.transition(request.incident_id, revision, state)
    with pytest.raises(SentinelDenied):
        await service.resolve_from_receipt(request.incident_id, revision, receipt)
    healthy = item.model_copy(
        update={
            "source_observation_id": str(uuid7()),
            "facts": Facts(condition="HEALTHY"),
        }
    )
    latest, identity = await service.ingest(healthy)
    await service.resolve_from_receipt(identity, revision + 1, latest)
    _, reopened = await service.ingest(
        item.model_copy(update={"source_observation_id": str(uuid7())})
    )
    assert reopened != identity


async def test_c24_cleanup_cannot_remove_live_owner(database, monkeypatch):
    executor, adapter, identity, *_ = await pipeline(database)
    claim = await executor.claim(identity, uuid7())
    with monkeypatch.context() as patch:
        coordinate_transactions(patch, database)
        results = await asyncio.gather(executor.cleanup(), executor.prepare_dispatch(claim))
    assert results[0] == 0
    adapter.execute.assert_not_awaited()
    await executor.finish(claim, "SUCCEEDED")
    assert await executor.cleanup() == 0


async def test_c24_cleanup_races_lease_recovery(database, monkeypatch):
    executor, adapter, identity, *_ = await pipeline(database)
    old = await expired_claim(executor, identity)
    original = database.transaction
    barrier = asyncio.Barrier(2)
    arrivals = 0

    @asynccontextmanager
    async def initial_transactions():
        nonlocal arrivals
        arrivals += 1
        synchronize = arrivals <= 2
        async with original() as session:
            if synchronize:
                async with asyncio.timeout(10):
                    await barrier.wait()
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(database, "transaction", initial_transactions)
        cleaned, recovered = await asyncio.gather(
            executor.cleanup(), executor.claim(identity, uuid7()), return_exceptions=True
        )
    if isinstance(recovered, ExecutionClaim):
        assert cleaned == 0
        assert await executor.dispatch(recovered) == "SUCCEEDED"
        assert adapter.execute.await_count == 1
    else:
        assert isinstance(recovered, SentinelDenied)
        assert cleaned == 1
        adapter.execute.assert_not_awaited()
    with pytest.raises(SentinelDenied):
        await executor.prepare_dispatch(old)
