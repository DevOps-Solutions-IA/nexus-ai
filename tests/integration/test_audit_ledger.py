import datetime as dt
import uuid

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_chain_replay_conflict_and_verification(tenant_database, make_organization):
    from nexus_ai.audit.contracts import AuditActor, AuditConflict, AuditIntent
    from nexus_ai.audit.repository import AuditRepository

    organization = await make_organization()
    intent = AuditIntent(
        organization_id=organization.id,
        source_id=uuid.uuid4(),
        producer="customer",
        action="customer.created",
        target_type="customer",
        target_id=uuid.uuid4(),
        actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        occurred_at=dt.datetime.now(dt.UTC),
    )
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        record = await AuditRepository(tenant).append(intent)
        assert record.sequence == 1
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        duplicate = await AuditRepository(tenant).append(intent)
        assert duplicate.id == record.id
        with pytest.raises(AuditConflict):
            await AuditRepository(tenant).append(intent.model_copy(update={"outcome": "FAILED"}))
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        result = await AuditRepository(tenant).verify()
        assert result.valid and result.complete and result.checked == 1


async def test_runtime_immutable_and_rls(tenant_database, make_organization):
    from sqlalchemy.exc import DBAPIError

    from nexus_ai.audit.contracts import AuditActor, AuditIntent
    from nexus_ai.audit.repository import AuditRepository

    organization = await make_organization()
    other = await make_organization()
    intent = AuditIntent(
        organization_id=organization.id,
        source_id=uuid.uuid4(),
        producer="customer",
        action="customer.created",
        target_type="customer",
        target_id=uuid.uuid4(),
        actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        occurred_at=dt.datetime.now(dt.UTC),
    )
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        record = await AuditRepository(tenant).append(intent)
    for statement in ("UPDATE audit_records SET sequence=2", "DELETE FROM audit_records"):
        with pytest.raises(DBAPIError):
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                await tenant.session.execute(text(statement))
    async with tenant_database.tenant_transaction(other.id) as tenant:
        assert await AuditRepository(tenant).get(record.id) is None


async def test_missing_head_never_certifies_existing_facts(tenant_database, make_organization):
    import asyncpg

    from nexus_ai.audit.contracts import AuditActor, AuditIntent
    from nexus_ai.audit.repository import AuditRepository
    from tests.conftest import SUPERUSER_DSN

    organization = await make_organization()
    intent = AuditIntent(
        organization_id=organization.id,
        source_id=uuid.uuid4(),
        producer="customer",
        action="customer.created",
        target_type="customer",
        target_id=uuid.uuid4(),
        actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        occurred_at=dt.datetime.now(dt.UTC),
    )
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await AuditRepository(tenant).append(intent)
    control = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await control.execute("DELETE FROM audit_heads WHERE organization_id=$1", organization.id)
    finally:
        await control.close()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        verification = await AuditRepository(tenant).verify()
        assert not verification.valid and verification.error == "HEAD_MISSING"
