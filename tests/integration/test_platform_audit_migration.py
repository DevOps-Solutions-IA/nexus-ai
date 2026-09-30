"""Corrective migration preserves immutable legacy facts and refuses destructive collapse."""

import asyncio
import datetime as dt
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from nexus_ai.audit.contracts import GENESIS, record_digest
from nexus_ai.audit.platform.config import PlatformAuditSettings
from nexus_ai.audit.platform.contracts import (
    PlatformAuditActor,
    PlatformAuditIntent,
    semantic_digest,
)
from nexus_ai.audit.platform.control import PlatformAuditControl
from nexus_ai.audit.platform.database import PlatformAuditDatabase
from nexus_ai.audit.platform.worker import PlatformAuditWorker
from tests.conftest import MIGRATION_DSN, RUNTIME_DSN, SUPERUSER_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_platform_corrective_preserves_legacy_and_refuses_populated_downgrade(
    migrated_database,
):
    name = f"nxs_p22_corrective_{uuid4().hex}"
    control = await asyncpg.connect(SUPERUSER_DSN)
    await control.execute(f'CREATE DATABASE "{name}" OWNER nexus_migration')
    environment = dict(os.environ)
    environment["NXS_ENVIRONMENT"] = "test"
    environment["NXS_DATABASE__MIGRATION_DSN"] = (
        make_url(MIGRATION_DSN).set(database=name).render_as_string(hide_password=False)
    )
    environment["NXS_DATABASE__DSN"] = (
        make_url(RUNTIME_DSN).set(database=name).render_as_string(hide_password=False)
    )

    async def run(*arguments, succeeds=True):
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "alembic",
            *arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(120):
            output, _ = await process.communicate()
        assert (process.returncode == 0) is succeeds, output.decode()[-6000:]
        return output.decode()

    connection = None
    try:
        await run("upgrade", "d22b0b1c2d3e")
        connection = await asyncpg.connect(
            make_url(SUPERUSER_DSN).set(database=name).render_as_string(hide_password=False)
        )
        source_id, record_id = uuid4(), uuid4()
        instant = dt.datetime.now(dt.UTC)
        intent = PlatformAuditIntent(
            source_id=source_id,
            action="sentinel.proposal.recorded",
            target_type="proposal",
            target_id=uuid4(),
            actor=PlatformAuditActor(kind="SERVICE", service="sentinel-store"),
            occurred_at=instant,
        )
        fact = {
            "integrity_version": 1,
            "scope": "PLATFORM",
            "organization_id": None,
            "id": str(record_id),
            "sequence": 1,
            "predecessor": GENESIS,
            "recorded_at": instant.isoformat(),
            "intent": intent.model_dump(mode="json"),
        }
        digest = record_digest(fact)
        await connection.execute(
            "INSERT INTO platform_audit_intents(id,payload) VALUES($1,$2)",
            source_id,
            intent.model_dump_json(),
        )
        await connection.execute(
            "INSERT INTO platform_audit_records"
            "(id,source_id,sequence,recorded_at,semantic_digest,predecessor,digest,fact) "
            "VALUES($1,$2,1,$3,$4,$5,$6,$7)",
            record_id,
            source_id,
            instant,
            semantic_digest(intent),
            GENESIS,
            digest,
            json.dumps(fact),
        )
        await connection.execute(
            "INSERT INTO platform_audit_receipts(source_id,record_id) VALUES($1,$2)",
            source_id,
            record_id,
        )
        await connection.execute("UPDATE platform_audit_heads SET sequence=1,digest=$1", digest)
        before = await connection.fetchrow("SELECT fact::text,digest FROM platform_audit_records")
        await run("upgrade", "head")
        assert await connection.fetchval("SELECT count(*) FROM platform_audit_heads") == 17
        assert (
            await connection.fetchval("SELECT domain FROM platform_audit_records")
            == "platform:v1:legacy"
        )
        assert (
            await connection.fetchval("SELECT source_role FROM platform_audit_intents")
            == "nexus_sentinel"
        )
        assert (
            await connection.fetchrow("SELECT fact::text,digest FROM platform_audit_records")
            == before
        )
        await run("check")
        platform = PlatformAuditDatabase(
            PlatformAuditSettings(
                database_dsn=SecretStr(
                    make_url(RUNTIME_DSN)
                    .set(
                        database=name,
                        username="nexus_audit_platform",
                        password="local-audit-platform-only",
                    )
                    .render_as_string(hide_password=False)
                )
            )
        )
        try:
            verified = await PlatformAuditControl(AsyncMock(), platform).verify(
                "authorized-test", domain="platform:v1:legacy"
            )
            assert verified["complete"] is True and verified["through"] == 1
            assert await PlatformAuditWorker(platform).process(source_id) == record_id
        finally:
            await platform.close()
        await run("downgrade", "d22b0b1c2d3e")
        assert (
            await connection.fetchrow("SELECT fact::text,digest FROM platform_audit_records")
            == before
        )
        await run("upgrade", "head")
        await run("check")
        await connection.execute("UPDATE platform_audit_heads SET sequence=1 WHERE id=2")
        refusal = await run("downgrade", "d22b0b1c2d3e", succeeds=False)
        assert "cannot collapse populated platform domains" in refusal
        assert await connection.fetchval("SELECT count(*) FROM platform_audit_heads") == 17
        assert (
            await connection.fetchrow("SELECT fact::text,digest FROM platform_audit_records")
            == before
        )
    finally:
        if connection is not None:
            await connection.close()
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await control.close()
