"""Real PostgreSQL platform provenance, recovery, concurrency and authority."""

import asyncio
import datetime as dt
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid7

import asyncpg
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from nexus_ai.audit.contracts import AuditActor, AuditConflict, AuditIntent, AuditProvenanceError
from nexus_ai.audit.platform.config import PlatformAuditSettings
from nexus_ai.audit.platform.contracts import PlatformAuditActor, PlatformAuditIntent
from nexus_ai.audit.platform.control import (
    PlatformAuditAuthority,
    PlatformAuditControl,
    PlatformAuditPermission,
)
from nexus_ai.audit.platform.database import PlatformAuditDatabase
from nexus_ai.audit.platform.producer import emit_platform_audit
from nexus_ai.audit.platform.worker import PlatformAuditWorker
from nexus_ai.audit.repository import AuditRepository
from nexus_ai.core.errors import UserInactiveError
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.domain.platform_audit.models import (
    PlatformAuditHead,
    PlatformAuditIntentRecord,
    PlatformAuditReceipt,
    PlatformAuditRecord,
)
from tests.conftest import RUNTIME_DSN, SUPERUSER_DSN
from tests.integration.test_sentinel_persistence import database as database

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def platform(migrated_database):
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await admin.execute(
            "TRUNCATE platform_audit_receipts, platform_audit_records, platform_audit_intents"
        )
        await admin.execute("UPDATE platform_audit_heads SET sequence=0, digest=repeat('0',64)")
    finally:
        await admin.close()
    dsn = make_url(RUNTIME_DSN).set(
        username="nexus_audit_platform", password="local-audit-platform-only"
    )
    connection = PlatformAuditDatabase(
        PlatformAuditSettings(database_dsn=SecretStr(dsn.render_as_string(hide_password=False)))
    )
    try:
        yield connection
    finally:
        await connection.close()


async def source(database, *, identity=None):
    async with database.transaction() as session:
        return await emit_platform_audit(
            session,
            action="sentinel.proposal.recorded",
            target_type="proposal",
            target_id=uuid7(),
            source_id=identity,
            actor=PlatformAuditActor(kind="SERVICE", service="sentinel-store"),
        )


def control(platform):
    return PlatformAuditControl(AsyncMock(), platform)


async def test_committed_source_replay_conflict_and_scope(platform, database):
    identity = await source(database)
    worker = PlatformAuditWorker(platform)
    record = await worker.process(identity)
    assert await worker.process(identity) == record
    assert await worker.run_once() == 0
    async with platform.transaction() as session:
        intent = PlatformAuditIntent.model_validate(
            (await session.get(PlatformAuditIntentRecord, identity)).payload
        )
        assert (await session.get(PlatformAuditReceipt, identity)).record_id == record
    with pytest.raises(AuditConflict):
        await worker.process(identity, expected=intent.model_copy(update={"outcome": "FAILED"}))
    with pytest.raises(ValidationError):
        PlatformAuditIntent.model_validate({**intent.model_dump(), "organization_id": uuid7()})
    with pytest.raises(AuditProvenanceError):
        await worker.process(uuid7())
    verified = await control(platform).verify("token")
    assert verified["complete"] and verified["through"] == 1


async def test_source_transaction_rollback_and_committed_visibility(platform, database):
    identity = uuid7()
    async with database.transaction() as session:
        await emit_platform_audit(
            session,
            action="sentinel.proposal.recorded",
            target_type="proposal",
            target_id=uuid7(),
            source_id=identity,
            actor=PlatformAuditActor(kind="SERVICE", service="sentinel-store"),
        )
        with pytest.raises(AuditProvenanceError):
            await PlatformAuditWorker(platform).process(identity)
    assert await PlatformAuditWorker(platform).run_once() == 1
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction() as session:
            await emit_platform_audit(
                session,
                action="sentinel.proposal.recorded",
                target_type="proposal",
                target_id=uuid7(),
                actor=PlatformAuditActor(kind="SERVICE", service="sentinel-store"),
            )
            raise RuntimeError("rollback")
    assert await PlatformAuditWorker(platform).run_once() == 0


async def test_worker_crash_before_commit_and_lost_response(platform, database, monkeypatch):
    identity = await source(database)
    transaction = platform.transaction

    @asynccontextmanager
    async def crash():
        async with transaction() as session:
            yield session
            await session.flush()
            raise RuntimeError("before commit")

    monkeypatch.setattr(platform, "transaction", crash)
    with pytest.raises(RuntimeError, match="before commit"):
        await PlatformAuditWorker(platform).process(identity)
    monkeypatch.setattr(platform, "transaction", transaction)
    async with platform.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(PlatformAuditRecord)) == 0
        assert await session.get(PlatformAuditReceipt, identity) is None
        assert (await session.get(PlatformAuditHead, 1)).sequence == 0
    committed = await PlatformAuditWorker(platform).process(identity)
    assert await PlatformAuditWorker(platform).process(identity) == committed


async def test_concurrent_appends_and_duplicates_converge(platform, database):
    identities = [await source(database) for _ in range(8)]
    barrier = asyncio.Barrier(12)

    async def append(identity):
        await barrier.wait()
        return await PlatformAuditWorker(platform).process(identity)

    results = await asyncio.gather(*(append(identity) for identity in identities + identities[:4]))
    assert len(set(results)) == 8
    verified = await control(platform).verify("token")
    assert verified["through"] == 8 and verified["complete"]


async def test_stable_bounded_pagination_and_partial_verification(platform, database):
    worker = PlatformAuditWorker(platform)
    for _ in range(3):
        await worker.process(await source(database))
    query = control(platform)
    page = await query.records("token", limit=1)
    await worker.process(await source(database))
    continuation = await query.records(
        "token", after=page["next_after"], high_water=page["high_water"]
    )
    assert len(continuation["records"]) == 2
    verified = await query.verify("token", limit=2)
    assert not verified["complete"] and verified["through"] == 2
    assert (await query.verify("token", after=2, high_water=4))["through"] == 4
    for limit in (0, 201, True):
        with pytest.raises(ValueError):
            await query.records("token", limit=limit)


async def test_role_privileges_and_immutable_triggers(platform, database):
    identity = await source(database)
    await PlatformAuditWorker(platform).process(identity)
    for statement in (
        "SELECT * FROM organizations",
        "SELECT * FROM audit_records",
        "SELECT * FROM sentinel_incidents",
        "INSERT INTO platform_audit_heads VALUES (2,0,'x')",
        "UPDATE platform_audit_records SET sequence=10",
        "DELETE FROM platform_audit_receipts",
        "UPDATE platform_audit_intents SET payload='{}'",
        "CREATE TABLE public.audit_escape(id int)",
    ):
        with pytest.raises(DBAPIError):
            async with platform.transaction() as session:
                await session.execute(text(statement))
    with pytest.raises(DBAPIError):
        async with database.transaction() as session:
            await session.execute(text("SELECT * FROM platform_audit_intents"))
    runtime = await asyncpg.connect(RUNTIME_DSN.replace("postgresql+asyncpg://", "postgresql://"))
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        for statement in (
            "SELECT * FROM platform_audit_intents",
            "SELECT * FROM platform_audit_records",
            "SELECT * FROM platform_audit_heads",
            "SELECT * FROM platform_audit_receipts",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await runtime.fetch(statement)
        for statement in (
            "DELETE FROM platform_audit_intents",
            "DELETE FROM platform_audit_records",
            "DELETE FROM platform_audit_receipts",
        ):
            with pytest.raises(asyncpg.CheckViolationError, match="immutable"):
                await admin.execute(statement)
    finally:
        await runtime.close()
        await admin.close()


async def test_live_platform_grants_and_revocation(
    platform, auth_client, make_auth_org, make_auth_user, login_helper
):
    organization = await make_auth_org()
    email, password, user = await make_auth_user(organization=organization)
    token = (await login_helper(email, password))["access_token"]
    resources = auth_client.nexus_app.state.lifespan.resources
    authority = PlatformAuditAuthority(
        resources.token_service, PrincipalStateValidator(resources.database), resources.database
    )
    query = PlatformAuditControl(authority, platform)
    with pytest.raises(AuditProvenanceError, match="grant"):
        await query.records(token)
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await admin.execute(
            "INSERT INTO platform_grants(user_id,capability) VALUES($1,$2)",
            user.id,
            "audit:platform:read",
        )
        assert (await query.records(token))["records"] == []
        with pytest.raises(AuditProvenanceError):
            await query.verify(token)
        await admin.execute("DELETE FROM platform_grants WHERE user_id=$1", user.id)
        with pytest.raises(AuditProvenanceError):
            await query.records(token)
        await admin.execute(
            "INSERT INTO platform_grants(user_id,capability) VALUES($1,$2)",
            user.id,
            "audit:platform:read",
        )
        await admin.execute("UPDATE users SET status='SUSPENDED' WHERE id=$1", user.id)
        with pytest.raises(UserInactiveError):
            await authority.require(token, PlatformAuditPermission.READ)
    finally:
        await admin.execute("DELETE FROM platform_grants WHERE user_id=$1", user.id)
        await admin.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"scope": "PLATFORM"},
        {"scope": None, "organization_id": None},
        {"scope": "TENANT", "organization_id": None},
        {"scope": "PLATFORM", "organization_id": "forged"},
    ],
)
async def test_database_rejects_missing_or_wrong_scope(platform, database, payload):
    with pytest.raises(DBAPIError):
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO platform_audit_intents(id,payload) "
                    "VALUES(:id,CAST(:payload AS jsonb))"
                ),
                {"id": uuid7(), "payload": json.dumps(payload)},
            )


async def test_terminal_poison_does_not_block_next_fact(platform, database):
    identity = uuid7()
    async with database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_audit_intents(id,payload) VALUES(:id,CAST(:payload AS jsonb))"
            ),
            {"id": identity, "payload": json.dumps({"scope": "PLATFORM", "organization_id": None})},
        )
    await source(database)
    assert await PlatformAuditWorker(platform).run_once() == 2
    assert await PlatformAuditWorker(platform).run_once() == 0
    async with platform.transaction() as session:
        receipt = await session.get(PlatformAuditReceipt, identity)
        assert receipt.record_id is None and receipt.reason_code == "INVALID_INTENT"
        assert await session.scalar(select(func.count()).select_from(PlatformAuditRecord)) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE platform_audit_records SET semantic_digest=repeat('f',64) WHERE sequence=1",
        "UPDATE platform_audit_records SET predecessor=repeat('f',64) WHERE sequence=2",
        "DELETE FROM platform_audit_records WHERE sequence=2",
    ],
)
async def test_privileged_corruption_detected_in_page_or_anchor(platform, database, statement):
    worker = PlatformAuditWorker(platform)
    for _ in range(3):
        await worker.process(await source(database))
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        async with admin.transaction():
            await admin.execute("SET LOCAL session_replication_role=replica")
            await admin.execute(statement)
    finally:
        await admin.close()
    with pytest.raises(AuditProvenanceError):
        await control(platform).verify("token", after=1, high_water=3)


async def test_invalid_receipt_and_outer_scope_fail_database(platform, database):
    identity = await source(database)
    with pytest.raises(DBAPIError):
        async with platform.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO platform_audit_receipts(source_id,record_id,reason_code) "
                    "VALUES(:id,NULL,NULL)"
                ),
                {"id": identity},
            )
    await PlatformAuditWorker(platform).process(identity)
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        async with admin.transaction():
            await admin.execute(
                "ALTER TABLE platform_audit_records DISABLE TRIGGER immutable_platform_audit"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with admin.transaction():
                    await admin.execute(
                        "UPDATE platform_audit_records "
                        "SET fact=jsonb_set(fact,'{scope}','\"TENANT\"')"
                    )
            await admin.execute(
                "ALTER TABLE platform_audit_records ENABLE TRIGGER immutable_platform_audit"
            )
    finally:
        await admin.close()


async def test_independent_platform_append_while_tenant_head_locked(
    platform, database, tenant_database, make_organization
):
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
    identity = await source(database)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await AuditRepository(tenant).append(intent)
        record = await asyncio.wait_for(PlatformAuditWorker(platform).process(identity), timeout=2)
        assert record is not None
    assert (await control(platform).verify("token"))["complete"]


async def test_snapshot_verification_excludes_concurrent_append(platform, database, monkeypatch):
    worker = PlatformAuditWorker(platform)
    await worker.process(await source(database))
    next_source = await source(database)
    snapshot_open = asyncio.Event()
    release_snapshot = asyncio.Event()
    transaction = platform.transaction

    @asynccontextmanager
    async def barrier_transaction(*, snapshot=False):
        async with transaction(snapshot=snapshot) as session:
            if snapshot:
                snapshot_open.set()
                await release_snapshot.wait()
            yield session

    monkeypatch.setattr(platform, "transaction", barrier_transaction)
    verifying = asyncio.create_task(control(platform).verify("token"))
    await snapshot_open.wait()
    await worker.process(next_source)
    release_snapshot.set()
    verified = await verifying
    assert verified["high_water"] == 1 and verified["through"] == 1
    assert (await control(platform).verify("token"))["through"] == 2


async def test_changed_committed_source_cannot_hide_behind_receipt(platform, database):
    identity = await source(database)
    worker = PlatformAuditWorker(platform)
    await worker.process(identity)
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        async with admin.transaction():
            await admin.execute("SET LOCAL session_replication_role=replica")
            await admin.execute(
                "UPDATE platform_audit_intents SET "
                "payload=jsonb_set(payload,'{outcome}','\"FAILED\"') WHERE id=$1",
                identity,
            )
    finally:
        await admin.close()
    with pytest.raises(AuditConflict):
        await worker.process(identity)


async def test_actual_role_attributes_and_no_tenant_acl(platform):
    async with platform.transaction() as session:
        tables = list(
            await session.scalars(
                text(
                    "SELECT relname FROM pg_class JOIN pg_namespace "
                    "ON pg_namespace.oid=relnamespace "
                    "WHERE nspname='public' AND relkind='r' "
                    "AND has_table_privilege(current_user,pg_class.oid,'SELECT') ORDER BY relname"
                )
            )
        )
        assert tables == [
            "platform_audit_heads",
            "platform_audit_intents",
            "platform_audit_receipts",
            "platform_audit_records",
        ]
    await platform.close()
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await admin.execute("ALTER ROLE nexus_audit_platform BYPASSRLS")
        with pytest.raises(AuditProvenanceError, match="unsafe"):
            async with platform.transaction():
                pytest.fail("unsafe role was admitted")
    finally:
        await admin.execute("ALTER ROLE nexus_audit_platform NOBYPASSRLS")
        await admin.close()
