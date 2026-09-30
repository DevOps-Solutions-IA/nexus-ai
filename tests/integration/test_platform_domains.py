"""PostgreSQL proofs for bounded platform domains and transactional claims."""

import asyncio
from contextlib import asynccontextmanager
from types import MappingProxyType
from unittest.mock import AsyncMock
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

from nexus_ai.audit.contracts import AuditProvenanceError
from nexus_ai.audit.platform.contracts import domain_head_id, integrity_domain
from nexus_ai.audit.platform.control import PlatformAuditControl
from nexus_ai.audit.platform.worker import PlatformAuditWorker
from nexus_ai.domain.platform_audit.models import (
    PlatformAuditHead,
    PlatformAuditIntentRecord,
    PlatformAuditRecord,
)
from tests.conftest import SUPERUSER_DSN
from tests.integration.test_platform_audit import platform as platform
from tests.integration.test_platform_audit import source
from tests.integration.test_sentinel_persistence import database as database

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def different_target():
    return next(
        UUID(int=value)
        for value in range(2, 100)
        if integrity_domain("sentinel", UUID(int=value))
        != integrity_domain("sentinel", UUID(int=1))
    )


async def test_independent_domains_progress_while_one_head_is_locked(platform, database):
    first = await source(database)
    second = await source(database, target_id=different_target())
    async with platform.transaction() as session:
        head = await session.get(
            PlatformAuditHead,
            domain_head_id(integrity_domain("sentinel", UUID(int=1))),
            with_for_update=True,
        )
        assert head.sequence == 0
        await asyncio.wait_for(PlatformAuditWorker(platform).process(second), timeout=2)
    await PlatformAuditWorker(platform).process(first)
    async with platform.transaction() as session:
        rows = list(await session.scalars(select(PlatformAuditRecord)))
        assert len({row.domain for row in rows}) == 2
        assert [row.sequence for row in rows] == [1, 1]


async def test_claim_skips_locked_source_and_rollback_releases_claim(platform, database):
    first = await source(database)
    second = await source(database, target_id=different_target())
    with pytest.raises(RuntimeError, match="crash"):
        async with platform.transaction() as session:
            await session.get(PlatformAuditIntentRecord, first, with_for_update=True)
            assert await asyncio.wait_for(PlatformAuditWorker(platform).run_once(), timeout=2) == 1
            assert await session.scalar(select(PlatformAuditRecord.source_id)) == second
            raise RuntimeError("crash")
    assert await PlatformAuditWorker(platform).run_once() == 1
    assert await PlatformAuditWorker(platform).run_once() == 0


async def test_busy_domain_does_not_block_claiming_healthy_domain(platform, database):
    first = await source(database)
    second = await source(database, target_id=different_target())
    async with platform.transaction() as session:
        await session.get(
            PlatformAuditHead,
            domain_head_id(integrity_domain("sentinel", UUID(int=1))),
            with_for_update=True,
        )
        assert await asyncio.wait_for(PlatformAuditWorker(platform).run_once(), timeout=2) == 1
        assert await session.scalar(select(PlatformAuditRecord.source_id)) == second
    assert await PlatformAuditWorker(platform).run_once() == 1
    async with platform.transaction() as session:
        assert set(await session.scalars(select(PlatformAuditRecord.source_id))) == {first, second}


@pytest.mark.parametrize("domain", ["platform:v2:10", "forged", "platform:v1:legacy"])
async def test_database_rejects_unknown_or_misbound_domain(platform, database, domain):
    await PlatformAuditWorker(platform).process(await source(database))
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        async with admin.transaction():
            await admin.execute("SET LOCAL session_replication_role=replica")
            with pytest.raises(asyncpg.CheckViolationError):
                async with admin.transaction():
                    await admin.execute("UPDATE platform_audit_records SET domain=$1", domain)
    finally:
        await admin.close()
    pending = await source(database)
    from uuid import uuid7

    async with platform.transaction() as session:
        existing = await session.scalar(select(PlatformAuditRecord))
        values = {
            column.name: getattr(existing, column.name)
            for column in PlatformAuditRecord.__table__.columns
        }
    values.update(id=uuid7(), source_id=pending, sequence=100, domain=domain)
    with pytest.raises(DBAPIError, match="integrity_domain"):
        async with platform.transaction() as session:
            await session.execute(insert(PlatformAuditRecord).values(**values))


async def test_two_workers_hold_disjoint_claims_and_heads_concurrently(
    platform, database, monkeypatch
):
    await source(database)
    await source(database, target_id=different_target())
    worker = PlatformAuditWorker(platform)
    append = worker._append
    barrier = asyncio.Barrier(2)

    async def simultaneous_append(*args, **kwargs):
        result = await append(*args, **kwargs)
        await barrier.wait()
        return result

    monkeypatch.setattr(worker, "_append", simultaneous_append)
    counts = await asyncio.wait_for(asyncio.gather(worker.run_once(), worker.run_once()), timeout=3)
    assert counts == [1, 1]


async def test_locked_platform_head_leaves_tenant_and_other_domain_independent(
    platform,
    database,
    tenant_database,
    make_organization,
):
    import datetime as dt
    from uuid import uuid7

    from nexus_ai.audit.contracts import AuditActor, AuditIntent
    from nexus_ai.audit.repository import AuditRepository

    organization = await make_organization()
    intent = AuditIntent(
        organization_id=organization.id,
        source_id=uuid7(),
        producer="customer",
        action="customer.created",
        target_type="customer",
        target_id=uuid7(),
        actor=AuditActor(kind="HUMAN", user_id=uuid7()),
        occurred_at=dt.datetime.now(dt.UTC),
    )
    other = await source(database, target_id=different_target())
    async with platform.transaction() as session:
        await session.get(
            PlatformAuditHead,
            domain_head_id(integrity_domain("sentinel", UUID(int=1))),
            with_for_update=True,
        )
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await asyncio.wait_for(AuditRepository(tenant).append(intent), timeout=2)
        await asyncio.wait_for(PlatformAuditWorker(platform).process(other), timeout=2)


async def test_poison_source_in_one_domain_leaves_other_domain_healthy(platform, database):
    import datetime as dt
    from uuid import uuid7

    from nexus_ai.audit.platform.contracts import PlatformAuditActor, PlatformAuditIntent
    from nexus_ai.domain.platform_audit.models import PlatformAuditReceipt

    poison = PlatformAuditIntent(
        source_id=uuid7(),
        action="sentinel.proposal.recorded",
        target_type="proposal",
        target_id=UUID(int=1),
        actor=PlatformAuditActor(kind="SERVICE", service="sentinel-store"),
        occurred_at=dt.datetime.now(dt.UTC),
    ).model_copy(update={"action": "unregistered.action"})
    async with database.transaction() as session:
        await session.execute(
            insert(PlatformAuditIntentRecord).values(
                id=poison.source_id, payload=poison.model_dump(mode="json")
            )
        )
    healthy = await source(database, target_id=different_target())
    assert await PlatformAuditWorker(platform).run_once() == 2
    async with platform.transaction() as session:
        assert (
            await session.get(PlatformAuditReceipt, poison.source_id)
        ).reason_code == "INVALID_INTENT"
        record = await session.scalar(select(PlatformAuditRecord))
        assert record.source_id == healthy
        assert record.domain != integrity_domain("sentinel", poison.target_id)


async def test_concurrent_claimers_commit_each_source_once(platform, database):
    for value in range(20):
        await source(database, target_id=UUID(int=value))
    barrier = asyncio.Barrier(3)

    async def consume():
        await barrier.wait()
        return await PlatformAuditWorker(platform).run_once()

    counts = await asyncio.gather(*(consume() for _ in range(3)))
    assert sum(counts) == 20
    async with platform.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(PlatformAuditRecord)) == 20


async def test_same_domain_contention_remains_contiguous(platform, database):
    identities = [await source(database) for _ in range(32)]
    barrier = asyncio.Barrier(8)

    async def append_batch(offset):
        await barrier.wait()
        for identity in identities[offset::8]:
            await PlatformAuditWorker(platform).process(identity)

    await asyncio.gather(*(append_batch(offset) for offset in range(8)))
    domain = integrity_domain("sentinel", UUID(int=1))
    query = PlatformAuditControl(AsyncMock(), platform)
    result = await query.verify("token", domain=domain)
    assert result["complete"] and result["through"] == 32


async def test_claim_append_receipt_and_head_rollback_together(platform, database, monkeypatch):
    from nexus_ai.domain.platform_audit.models import PlatformAuditReceipt

    identity = await source(database)
    transaction = platform.transaction

    @asynccontextmanager
    async def crash_before_commit():
        async with transaction() as session:
            yield session
            await session.flush()
            raise RuntimeError("claim commit crash")

    monkeypatch.setattr(platform, "transaction", crash_before_commit)
    with pytest.raises(RuntimeError, match="claim commit crash"):
        await PlatformAuditWorker(platform).run_once()
    monkeypatch.setattr(platform, "transaction", transaction)
    async with platform.transaction() as session:
        assert await session.get(PlatformAuditReceipt, identity) is None
        assert await session.scalar(select(func.count()).select_from(PlatformAuditRecord)) == 0
        assert await session.scalar(select(func.sum(PlatformAuditHead.sequence))) == 0
    assert await PlatformAuditWorker(platform).run_once() == 1


async def test_claim_update_privilege_cannot_mutate_source(platform, database):
    await source(database)
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="immutable"):
        async with platform.transaction() as session:
            await session.execute(text("UPDATE platform_audit_intents SET id=id"))


async def test_multi_domain_snapshot_and_corruption_are_isolated(platform, database, monkeypatch):
    worker = PlatformAuditWorker(platform)
    await worker.process(await source(database))
    other_target = different_target()
    await worker.process(await source(database, target_id=other_target))
    pending = await source(database, target_id=other_target)
    query = PlatformAuditControl(AsyncMock(), platform)
    domains = [entry["domain"] for entry in await query.domains("token")]
    opened, release = asyncio.Event(), asyncio.Event()
    transaction = platform.transaction

    @asynccontextmanager
    async def snapshot_barrier(*, snapshot=False):
        async with transaction(snapshot=snapshot) as session:
            if snapshot:
                opened.set()
                await release.wait()
            yield session

    monkeypatch.setattr(platform, "transaction", snapshot_barrier)
    verifying = asyncio.create_task(query.verify_domains("token", domains=domains))
    await opened.wait()
    await worker.process(pending)
    release.set()
    verified = await verifying
    assert verified["complete"] and verified["all_domains"]
    assert sum(entry["through"] for entry in verified["domains"]) == 2
    monkeypatch.setattr(platform, "transaction", transaction)
    corrupt_domain = integrity_domain("sentinel", other_target)
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        async with admin.transaction():
            await admin.execute("SET LOCAL session_replication_role=replica")
            await admin.execute(
                "UPDATE platform_audit_records SET digest=repeat('f',64) WHERE domain=$1",
                corrupt_domain,
            )
    finally:
        await admin.close()
    assert (await query.verify("token", domain=integrity_domain("sentinel", UUID(int=1))))[
        "complete"
    ]
    with pytest.raises(AuditProvenanceError):
        await query.verify_domains("token", domains=domains)


async def test_registry_extension_has_distinct_db_authority_and_metadata(
    platform, database, monkeypatch
):
    import datetime as dt
    from uuid import uuid7

    from pydantic import BaseModel, ConfigDict

    from nexus_ai.audit.platform import contracts, producer, worker

    class TestMetadata(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        generation: int

    specification = contracts.ProducerSpecification(
        owning_subsystem="test-fixture",
        source_role="nexus_audit_test_source",
        actions=MappingProxyType({"test-fixture.changed": "fixture"}),
        services=frozenset({"test-fixture-service"}),
        human_actor_allowed=False,
        metadata_fields=frozenset({"generation"}),
        metadata_model=TestMetadata,
    )
    registry = MappingProxyType({**contracts.PRODUCERS, "test-fixture": specification})
    for module in (contracts, producer, worker):
        monkeypatch.setattr(module, "PRODUCERS", registry)
    identity = uuid7()
    intent = contracts.PlatformAuditIntent(
        source_id=identity,
        producer="test-fixture",
        action="test-fixture.changed",
        target_type="fixture",
        target_id=uuid7(),
        actor=contracts.PlatformAuditActor(kind="SERVICE", service="test-fixture-service"),
        metadata={"generation": 3},
        occurred_at=dt.datetime.now(dt.UTC),
    )
    async with database.transaction() as session:
        with pytest.raises(AuditProvenanceError):
            await producer.emit_platform_audit(
                session,
                producer="test-fixture",
                action=intent.action,
                target_type=intent.target_type,
                target_id=intent.target_id,
                actor=intent.actor,
                metadata=intent.metadata,
            )
        await session.execute(
            text(
                "INSERT INTO platform_audit_intents(id,payload,source_role) "
                "VALUES(:id,CAST(:payload AS jsonb),'nexus_audit_test_source')"
            ),
            {"id": identity, "payload": intent.model_dump_json()},
        )
    assert await worker.PlatformAuditWorker(platform).run_once() == 1
    from nexus_ai.domain.platform_audit.models import PlatformAuditReceipt

    async with platform.transaction() as session:
        assert (
            await session.get(PlatformAuditIntentRecord, identity)
        ).source_role == "nexus_sentinel"
        assert (await session.get(PlatformAuditReceipt, identity)).reason_code == "INVALID_INTENT"
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await admin.execute(
            "CREATE ROLE nexus_audit_test_source LOGIN PASSWORD 'test-source-only' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute("GRANT USAGE ON SCHEMA public TO nexus_audit_test_source")
        await admin.execute("GRANT INSERT ON platform_audit_intents TO nexus_audit_test_source")
        from sqlalchemy.engine import make_url

        dsn = make_url(SUPERUSER_DSN).set(
            username="nexus_audit_test_source", password="test-source-only"
        )
        connection = await asyncpg.connect(dsn.render_as_string(hide_password=False))
        try:
            legitimate = intent.model_copy(update={"source_id": uuid7()})
            await connection.execute(
                "INSERT INTO platform_audit_intents(id,payload) VALUES($1,$2::jsonb)",
                legitimate.source_id,
                legitimate.model_dump_json(),
            )
        finally:
            await connection.close()
        await worker.PlatformAuditWorker(platform).process(legitimate.source_id)
        async with platform.transaction() as session:
            record = await session.scalar(
                select(PlatformAuditRecord).where(
                    PlatformAuditRecord.source_id == legitimate.source_id
                )
            )
            assert record.fact["intent"]["metadata"] == {"generation": 3}
    finally:
        await admin.execute("DROP OWNED BY nexus_audit_test_source")
        await admin.execute("DROP ROLE nexus_audit_test_source")
        await admin.close()
